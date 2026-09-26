#!/usr/bin/env python3
"""Prepare the macosctl config/state namespace without deleting the svc namespace."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path


Entry = tuple[str, bytes | str, int, int, int]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class MigrationError(Exception):
    """The old namespace or destination cannot be migrated safely."""


def _validate_root(root: Path) -> Path:
    if not root.is_absolute() or ".." in root.parts:
        raise MigrationError("--root는 .. 없는 절대 경로여야 한다")
    try:
        root_info = root.lstat()
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MigrationError(f"migration root를 확정할 수 없다: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise MigrationError(f"migration root가 실제 디렉터리가 아니다: {root}")
    current = Path(canonical.anchor)
    for part in canonical.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise MigrationError(f"migration root가 없다: {canonical}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise MigrationError(f"migration root parent가 심링크다: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise MigrationError(f"migration root parent가 디렉터리가 아니다: {current}")
    return canonical


def _secure_parent_chain(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise MigrationError(f"namespace parent가 심링크다: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise MigrationError(f"namespace parent가 디렉터리가 아니다: {current}")


def _validate_live_alias(root: Path, name: str, expected_target: str) -> None:
    alias = root / name
    try:
        info = alias.lstat()
        target = os.readlink(alias)
    except (OSError, RuntimeError) as exc:
        raise MigrationError(f"macOS live alias를 확인할 수 없다: {alias}") from exc
    if not stat.S_ISLNK(info.st_mode) or target != expected_target:
        raise MigrationError(
            f"macOS live alias가 표준 target이 아니다: {alias} -> {target}"
        )


def _validate_info(info: os.stat_result, display: Path, *, allow_link: bool) -> None:
    if stat.S_ISLNK(info.st_mode):
        if allow_link:
            return
        raise MigrationError(f"심링크를 허용하지 않는 경로다: {display}")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise MigrationError(f"hard-link 파일은 이식하지 않는다: {display}")
        return
    if stat.S_ISDIR(info.st_mode):
        return
    raise MigrationError(f"지원하지 않는 inode 형식이다: {display}")


def _require_same_entry(
    observed: os.stat_result,
    acquired: os.stat_result,
    display: Path,
) -> None:
    if not os.path.samestat(observed, acquired):
        raise MigrationError(f"검사 중 namespace 항목이 바뀌었다: {display}")


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _scan_directory(
    directory_fd: int,
    display_root: Path,
    prefix: str,
    *,
    config: bool,
    result: dict[str, Entry],
    after_observe: Callable[[str], None] | None,
) -> None:
    try:
        names = sorted(entry.name for entry in os.scandir(directory_fd))
    except OSError as exc:
        display = display_root / prefix if prefix else display_root
        raise MigrationError(f"namespace 디렉터리를 읽을 수 없다: {display}: {exc}") from exc

    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        display = display_root / relative
        try:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise MigrationError(f"namespace 항목을 검사할 수 없다: {display}: {exc}") from exc
        _validate_info(observed, display, allow_link=config)
        if after_observe is not None:
            after_observe(relative)

        snapshot_name = "macosctl.toml" if config and relative == "svc.toml" else relative
        try:
            if stat.S_ISREG(observed.st_mode):
                child_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                try:
                    acquired = os.fstat(child_fd)
                    _require_same_entry(observed, acquired, display)
                    _validate_info(acquired, display, allow_link=False)
                    payload = _read_fd(child_fd)
                finally:
                    os.close(child_fd)
                result[snapshot_name] = (
                    "file",
                    payload,
                    stat.S_IMODE(acquired.st_mode),
                    acquired.st_uid,
                    acquired.st_gid,
                )
            elif stat.S_ISDIR(observed.st_mode):
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    acquired = os.fstat(child_fd)
                    _require_same_entry(observed, acquired, display)
                    _validate_info(acquired, display, allow_link=False)
                    result[snapshot_name] = (
                        "dir",
                        b"",
                        stat.S_IMODE(acquired.st_mode),
                        acquired.st_uid,
                        acquired.st_gid,
                    )
                    _scan_directory(
                        child_fd,
                        display_root,
                        relative,
                        config=config,
                        result=result,
                        after_observe=after_observe,
                    )
                finally:
                    os.close(child_fd)
            else:
                payload = os.readlink(name, dir_fd=directory_fd)
                confirmed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                _require_same_entry(observed, confirmed, display)
                _validate_info(confirmed, display, allow_link=True)
                result[snapshot_name] = (
                    "link",
                    payload,
                    stat.S_IMODE(confirmed.st_mode),
                    confirmed.st_uid,
                    confirmed.st_gid,
                )
        except MigrationError:
            raise
        except OSError as exc:
            raise MigrationError(
                f"검사 중 namespace 항목이 바뀌었다: {display}: {exc}"
            ) from exc


def _scan(
    root: Path,
    *,
    config: bool,
    _after_observe: Callable[[str], None] | None = None,
) -> tuple[dict[str, Entry], os.stat_result]:
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except FileNotFoundError as exc:
        raise MigrationError(f"구 namespace가 없다: {root}") from exc
    except OSError as exc:
        raise MigrationError(f"구 namespace를 안전하게 열 수 없다: {root}: {exc}") from exc
    try:
        return _scan_open_directory(
            root_fd,
            root,
            config=config,
            after_observe=_after_observe,
        )
    finally:
        os.close(root_fd)


def _scan_open_directory(
    root_fd: int,
    display: Path,
    *,
    config: bool,
    after_observe: Callable[[str], None] | None = None,
) -> tuple[dict[str, Entry], os.stat_result]:
    root_info = os.fstat(root_fd)
    _validate_info(root_info, display, allow_link=False)
    result: dict[str, Entry] = {}
    _scan_directory(
        root_fd,
        display,
        "",
        config=config,
        result=result,
        after_observe=after_observe,
    )
    required = {"policy.json", "macosctl.toml"} if config else set()
    missing = required - set(result)
    if missing:
        raise MigrationError("구 config 필수 파일이 없다: " + ", ".join(sorted(missing)))
    return result, root_info


def _scan_child_at(
    parent_fd: int,
    name: str,
    display: Path,
    *,
    config: bool,
) -> dict[str, Entry]:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_info(observed, display, allow_link=False)
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except (MigrationError, OSError) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"namespace를 안전하게 열 수 없다: {display}: {exc}") from exc
    try:
        _require_same_entry(observed, os.fstat(child_fd), display)
        entries, _ = _scan_open_directory(child_fd, display, config=config)
        return entries
    finally:
        os.close(child_fd)


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _relative_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in ("", ".", "..") for part in parts):
        raise MigrationError(f"안전하지 않은 namespace 상대 경로다: {relative}")
    return parts


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = _open_directory_at(current_fd, part)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    _validate_info(observed, Path(name), allow_link=False)
    if expected is not None:
        _require_same_entry(expected, observed, Path(name))
    directory_fd = _open_directory_at(parent_fd, name)
    try:
        acquired = os.fstat(directory_fd)
        _require_same_entry(observed, acquired, Path(name))
        for child_name in sorted(entry.name for entry in os.scandir(directory_fd)):
            child_info = os.stat(
                child_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(child_info.st_mode):
                _remove_tree_at(directory_fd, child_name, expected=child_info)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    return True


def _create_temporary_directory(parent_fd: int, name_prefix: str) -> str:
    for _ in range(128):
        name = f"{name_prefix}{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name
    raise MigrationError("namespace 임시 디렉터리 이름을 만들 수 없다")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("namespace 파일 쓰기가 진행되지 않았다")
        view = view[written:]


def _materialize_at(
    parent_fd: int,
    name_prefix: str,
    entries: dict[str, Entry],
    root_info: os.stat_result,
) -> tuple[str, os.stat_result]:
    temporary = _create_temporary_directory(parent_fd, name_prefix)
    try:
        temporary_fd = _open_directory_at(parent_fd, temporary)
    except Exception:
        _remove_tree_at(parent_fd, temporary)
        raise
    try:
        directories = sorted(
            ((key, value) for key, value in entries.items() if value[0] == "dir"),
            key=lambda item: (item[0].count("/"), item[0]),
        )
        for relative, _ in directories:
            parts = _relative_parts(relative)
            directory_parent_fd = _open_relative_directory(temporary_fd, parts[:-1])
            try:
                os.mkdir(parts[-1], 0o700, dir_fd=directory_parent_fd)
            finally:
                os.close(directory_parent_fd)
        for relative, (kind, payload, mode, uid, gid) in sorted(entries.items()):
            if kind == "dir":
                continue
            parts = _relative_parts(relative)
            entry_parent_fd = _open_relative_directory(temporary_fd, parts[:-1])
            try:
                if kind == "link":
                    if not isinstance(payload, str):
                        raise MigrationError(f"심링크 payload가 문자열이 아니다: {relative}")
                    os.symlink(payload, parts[-1], dir_fd=entry_parent_fd)
                    os.chown(
                        parts[-1],
                        uid,
                        gid,
                        dir_fd=entry_parent_fd,
                        follow_symlinks=False,
                    )
                    continue
                if not isinstance(payload, bytes):
                    raise MigrationError(f"파일 payload가 bytes가 아니다: {relative}")
                file_fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=entry_parent_fd,
                )
                try:
                    _write_all(file_fd, payload)
                    os.fchown(file_fd, uid, gid)
                    os.fchmod(file_fd, mode)
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            finally:
                os.close(entry_parent_fd)
        for relative, (_, _, mode, uid, gid) in sorted(
            directories,
            key=lambda item: (-item[0].count("/"), item[0]),
        ):
            directory_fd = _open_relative_directory(
                temporary_fd,
                _relative_parts(relative),
            )
            try:
                os.fchown(directory_fd, uid, gid)
                os.fchmod(directory_fd, mode)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.fchown(temporary_fd, root_info.st_uid, root_info.st_gid)
        os.fchmod(temporary_fd, stat.S_IMODE(root_info.st_mode))
        os.fsync(temporary_fd)
        temporary_info = os.fstat(temporary_fd)
        return temporary, temporary_info
    except Exception:
        os.close(temporary_fd)
        _remove_tree_at(parent_fd, temporary)
        raise
    finally:
        try:
            os.close(temporary_fd)
        except OSError:
            pass


def _same_content(
    left: dict[str, Entry],
    right: dict[str, Entry],
) -> bool:
    return left == right


def migrate(
    root: Path,
    *,
    dry_run: bool = False,
    live: bool = False,
) -> tuple[Path, Path]:
    """Copy old config/state to the new namespace and leave the old trees intact."""
    root = _validate_root(Path(root))
    logical_new_config = root / "etc" / "macosctl"
    logical_new_state = root / "var" / "db" / "macosctl"
    if live:
        _validate_live_alias(root, "etc", "private/etc")
        _validate_live_alias(root, "var", "private/var")
        _secure_parent_chain(root, Path("private/etc"))
        _secure_parent_chain(root, Path("private/var/db"))
        old_config = root / "private" / "etc" / "svc"
        old_state = root / "private" / "var" / "db" / "svc"
        new_config = root / "private" / "etc" / "macosctl"
        new_state = root / "private" / "var" / "db" / "macosctl"
    else:
        old_config = root / "etc" / "svc"
        old_state = root / "var" / "db" / "svc"
        new_config = logical_new_config
        new_state = logical_new_state
        for relative in (
            Path("etc"), Path("etc/svc"), Path("var"), Path("var/db"),
            Path("var/db/svc"),
        ):
            _secure_parent_chain(root, relative)
    config_entries, config_root_info = _scan(old_config, config=True)
    state_entries, state_root_info = _scan(old_state, config=False)
    destinations = (
        (new_config, config_entries, config_root_info, True),
        (new_state, state_entries, state_root_info, False),
    )
    parent_fds: list[int] = []
    pending: list[tuple[int, str, Path, dict[str, Entry], os.stat_result]] = []
    try:
        for destination, entries, source_info, is_config in destinations:
            try:
                parent_fd = os.open(destination.parent, _DIRECTORY_FLAGS)
            except OSError as exc:
                raise MigrationError(
                    f"새 namespace parent를 안전하게 열 수 없다: {destination.parent}: {exc}"
                ) from exc
            parent_fds.append(parent_fd)
            try:
                os.stat(
                    destination.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pending.append(
                    (parent_fd, destination.name, destination, entries, source_info)
                )
                continue
            existing = _scan_child_at(
                parent_fd,
                destination.name,
                destination,
                config=is_config,
            )
            if not _same_content(existing, entries):
                raise MigrationError(f"새 namespace가 구 namespace와 다르다: {destination}")

        if dry_run:
            return logical_new_config, logical_new_state

        temporaries: list[tuple[int, str, os.stat_result, str, Path]] = []
        published: list[tuple[int, str, os.stat_result, Path]] = []
        try:
            for parent_fd, name, destination, entries, source_info in pending:
                temporary, temporary_info = _materialize_at(
                    parent_fd,
                    f".{name}.migrate.",
                    entries,
                    source_info,
                )
                temporaries.append(
                    (parent_fd, temporary, temporary_info, name, destination)
                )
            for parent_fd, temporary, temporary_info, name, destination in temporaries:
                os.rename(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                published.append((parent_fd, name, temporary_info, destination))
                os.fsync(parent_fd)
            return logical_new_config, logical_new_state
        except OSError as exc:
            rollback_error: Exception | None = None
            for parent_fd, name, published_info, _ in reversed(published):
                try:
                    _remove_tree_at(parent_fd, name, expected=published_info)
                    os.fsync(parent_fd)
                except Exception as cleanup_exc:
                    rollback_error = cleanup_exc
                    break
            if rollback_error is not None:
                raise MigrationError(
                    f"새 namespace 게시 실패: {exc}; rollback 실패: {rollback_error}"
                ) from exc
            raise MigrationError(f"새 namespace 게시 실패: {exc}") from exc
        finally:
            for parent_fd, temporary, temporary_info, _, _ in temporaries:
                _remove_tree_at(parent_fd, temporary, expected=temporary_info)
    finally:
        for parent_fd in parent_fds:
            os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.root == Path("/"):
            if not args.live:
                raise MigrationError("root / 이식에는 --live가 필요하다")
            if os.geteuid() != 0:
                raise MigrationError("live namespace 이식에는 root가 필요하다")
            if os.environ.get("MACOSCTL_INSTALLER_MIGRATION") != "1":
                raise MigrationError("live namespace 이식은 install.sh --migrate-namespace로만 실행한다")
        elif args.live:
            raise MigrationError("--live는 root / 에만 쓸 수 있다")
        new_config, new_state = migrate(
            args.root,
            dry_run=args.dry_run,
            live=args.live,
        )
    except (MigrationError, OSError) as exc:
        print(f"namespace migration 오류: {exc}", file=sys.stderr)
        return 2
    prefix = "dry-run " if args.dry_run else ""
    print(f"{prefix}① {new_config}")
    print(f"{prefix}② {new_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
