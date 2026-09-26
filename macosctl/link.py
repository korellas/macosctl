"""Non-privileged conf.d composition operations (D5, D8, D9)."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from macosctl import confd, model, policy, validate


LEGACY_MASK_CONTENT = (
    b"# svc mask: remove this file with `svc unmask`, do not edit it\n"
    b"schema = 1\n"
    b"managed = false\n"
)
MASK_CONTENT = (
    b"# macosctl mask: remove this file with `macosctl unmask`, do not edit it\n"
    b"schema = 1\n"
    b"managed = false\n"
)
LOCAL_TEMPLATE = "# {name} 로컬 오버라이드\nschema = 1\n"
_PREFIXED = re.compile(r"^(?P<prefix>[1-4][0-9])-(?P<name>[a-z0-9-]+)$")
_NAME = re.compile(r"^[a-z0-9-]+$")


class LinkError(Exception):
    """A requested composition change is unsafe or invalid."""


@dataclass(frozen=True)
class Listing:
    name: str
    path: Path
    target: Path
    services: int | None
    broken: bool


@dataclass(frozen=True)
class Unlinked:
    path: Path
    dropins: tuple[Path, ...]
    purged: bool


def _fragment_basename(path: Path, name: str | None) -> tuple[str, str]:
    requested = name if name is not None else path.stem
    if requested.endswith(".toml"):
        requested = requested[:-5]
    if not requested or Path(requested).name != requested:
        raise LinkError(f"잘못된 조각 이름: {requested!r}")
    prefixed = _PREFIXED.fullmatch(requested)
    if prefixed:
        prefix = int(prefixed.group("prefix"))
        logical = prefixed.group("name")
        if not 10 <= prefix <= 40:
            raise LinkError(f"프로젝트 조각 접두사는 10~40이어야 한다: {requested}")
        return f"{requested}.toml", logical
    if re.match(r"^\d\d-", requested):
        raise LinkError(f"프로젝트 조각 접두사는 10~40이어야 한다: {requested}")
    if _NAME.fullmatch(requested) is None:
        raise LinkError(f"잘못된 조각 이름: {requested!r}")
    return f"30-{requested}.toml", requested


def _read_fragment(
    path: Path,
    defaults,
    *,
    defaults_location: str | None = None,
) -> tuple[model.MergedService, ...]:
    raw = confd._read(path)  # one parser/validation contract with the loader
    confd._check_schema(raw, path)
    confd._reject_policy_user(raw, path, "fragment")
    confd._reject_fields(raw, {"schema", "service", "scaffold"}, path)
    scaffold = raw.get("scaffold")
    if scaffold is not None:
        if not isinstance(scaffold, dict):
            raise model.MergeError(f"scaffold는 테이블이어야 한다 ({path.name})")
        confd._reject_policy_user(scaffold, path, "scaffold")
    entries = raw.get("service", [])
    if not isinstance(entries, list):
        raise model.MergeError(f"service는 테이블 배열이어야 한다 ({path.name})")
    services: list[model.MergedService] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise model.MergeError(f"service는 테이블이어야 한다 ({path.name})")
        service = confd._service(
            entry, defaults, path.name, str(path), defaults_location
        )
        if service.name in names:
            raise model.MergeError(
                f"서비스 {service.name!r} 중복 선언: {path.name}, {path.name}"
            )
        names.add(service.name)
        services.append(service)
    return tuple(services)


def _validate_candidate(path: Path, config_root: Path) -> None:
    root = Path(config_root)
    current = confd.load(root)
    candidate = _read_fragment(
        path,
        current.defaults,
        defaults_location=str(root / "macosctl.toml"),
    )
    existing = {service.name: service.source_of("name") for service in current.services}
    for service in candidate:
        if service.name in existing:
            raise model.MergeError(
                f"서비스 {service.name!r} 중복 선언: "
                f"{existing[service.name]}, {path.name}"
            )
    applied: list[model.MergedService] = []
    for service in candidate:
        if _NAME.fullmatch(service.name) is None:
            raise LinkError(f"조각의 서비스 이름 규약 오류: {path.name}")
        dropin_dir = root / "conf.d" / f"{service.name}.d"
        for dropin_path in sorted(dropin_dir.glob("*.toml")):
            service = confd._dropin(
                service,
                confd._read(dropin_path),
                dropin_path,
                current.defaults,
            )
        applied.append(service)
    combined = model.MergedModel(
        current.defaults, current.services + tuple(applied), current.warnings
    )
    policy_path = root / "policy.json"
    loaded_policy = (
        policy.read(policy_path)
        if root == confd.CONFIG_ROOT
        else policy.read_staging(policy_path)
    )
    combined = policy.bind(combined, loaded_policy)
    fatal = tuple(
        problem for problem in validate.check(
            combined.services,
            combined.defaults,
            label_policy=loaded_policy,
        )
        if problem.fatal
    )
    if fatal:
        raise LinkError(f"apply 사전 검증 실패 ({path.name})")


def link(path: Path | str, config_root: Path, name: str | None = None) -> Path:
    """Validate a project fragment completely, then attach one symlink."""
    source = Path(path).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise LinkError(f"조각을 읽을 수 없다: {source}: {exc}") from exc
    if not source.is_file():
        raise LinkError(f"조각이 일반 파일이 아니다: {source}")
    filename, _ = _fragment_basename(source, name)
    destination = Path(config_root) / "conf.d" / filename
    if os.path.lexists(destination):
        raise LinkError(f"대상 basename이 이미 있다: {destination.name}")

    # Nothing below this line mutates config_root until every parse/merge check
    # has passed. os.symlink is the single atomic publication operation.
    try:
        _validate_candidate(source, Path(config_root))
    except confd.FragmentUnreadable as exc:
        raise LinkError(f"조각 검증 실패 ({source.name}): 읽을 수 없는 설정 위치") from exc
    except model.MergeError as exc:
        raise LinkError(f"조각 검증 실패 ({source.name}): schema/필드 규약 오류") from exc
    except policy.PolicyRefused as exc:
        raise LinkError(f"조각 검증 실패 ({source.name}): policy 규약 오류") from exc
    except (OSError, TypeError, AttributeError, ValueError) as exc:
        raise LinkError(f"조각 검증 실패 ({source.name}): 필드 타입/입출력 오류") from exc
    try:
        os.symlink(source, destination)
    except FileExistsError as exc:
        raise LinkError(f"대상 basename이 이미 있다: {destination.name}") from exc
    except OSError as exc:
        raise LinkError(f"심링크를 만들 수 없다 ({destination}): {exc}") from exc
    return destination


def _linked_path(name: str, config_root: Path) -> tuple[Path, str]:
    root = Path(config_root) / "conf.d"
    requested = name[:-5] if name.endswith(".toml") else name
    prefixed = _PREFIXED.fullmatch(requested)
    if prefixed:
        prefix = int(prefixed.group("prefix"))
        if not 10 <= prefix <= 40:
            raise LinkError(f"프로젝트 조각 접두사는 10~40이어야 한다: {name}")
    elif re.match(r"^\d\d-", requested) or _NAME.fullmatch(requested) is None:
        raise LinkError(f"잘못된 조각 이름: {name!r}")
    exact = root / f"{requested}.toml"
    candidates = [exact] if prefixed else sorted(
        root.glob(f"??-{requested}.toml")
    )
    present = [path for path in candidates if os.path.lexists(path)]
    if not present:
        raise LinkError(f"연결된 조각이 없다: {name}")
    if len(present) != 1:
        raise LinkError(f"조각 이름이 모호하다: {name}")
    path = present[0]
    logical = re.sub(r"^\d\d-", "", path.stem)
    return path, logical


def _declared_names(path: Path) -> tuple[str, ...] | None:
    """Read only safe identity names; None means ownership cannot be proven."""
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return None
    entries = raw.get("service", []) if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        return None
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            return None
        names.append(name)
    return tuple(names)


def _candidate_dropin_dirs(
    path: Path,
    config_root: Path,
    declared: tuple[str, ...] | None,
) -> tuple[Path, ...]:
    conf_dir = Path(config_root) / "conf.d"
    if declared is not None:
        return tuple(
            conf_dir / f"{name}.d" for name in dict.fromkeys(declared)
            if os.path.lexists(conf_dir / f"{name}.d")
        )

    # A broken/unparseable target has unknown ownership. Warn conservatively,
    # excluding only directories proven to belong to another readable fragment.
    owned_elsewhere: set[str] = set()
    for fragment in sorted(conf_dir.glob("*.toml")):
        if fragment == path:
            continue
        names = _declared_names(fragment)
        if names is not None:
            owned_elsewhere.update(names)
    candidates: list[Path] = []
    for entry in sorted(conf_dir.iterdir()):
        if not entry.name.endswith(".d"):
            continue
        name = entry.name[:-2]
        if name not in owned_elsewhere:
            candidates.append(entry)
    return tuple(candidates)


def _open_conf_dir(config_root: Path) -> tuple[Path, int]:
    conf_dir = Path(config_root) / "conf.d"
    try:
        before = conf_dir.lstat()
    except OSError as exc:
        raise LinkError(f"conf.d를 확인할 수 없다: {conf_dir}") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise LinkError(f"conf.d가 실제 디렉터리가 아니다: {conf_dir}")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(conf_dir, flags)
    except OSError as exc:
        raise LinkError(f"conf.d를 안전하게 열 수 없다: {conf_dir}") from exc
    try:
        opened = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise LinkError(f"conf.d fd를 확인할 수 없다: {conf_dir}") from exc
    if (opened.st_dev, opened.st_ino, opened.st_mode) != (
        before.st_dev, before.st_ino, before.st_mode
    ):
        os.close(fd)
        raise LinkError(f"conf.d가 검사 중 바뀌었다: {conf_dir}")
    return conf_dir, fd


def _restore_quarantined_directory(
    directory_fd: int,
    quarantine: str,
    original: str,
) -> None:
    quarantine_stat = os.stat(
        quarantine, dir_fd=directory_fd, follow_symlinks=False
    )
    if not stat.S_ISDIR(quarantine_stat.st_mode):
        raise LinkError(f"격리 대상이 디렉터리가 아니다: {quarantine}")
    try:
        os.mkdir(
            original,
            stat.S_IMODE(quarantine_stat.st_mode),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LinkError(f"rollback 원래 이름이 이미 있다: {original}") from exc
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    quarantine_fd: int | None = None
    original_fd: int | None = None
    linked: list[str] = []
    try:
        quarantine_fd = os.open(quarantine, flags, dir_fd=directory_fd)
        original_fd = os.open(original, flags, dir_fd=directory_fd)
        names = tuple(os.listdir(quarantine_fd))
        for name in names:
            item = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                raise LinkError(f"rollback 대상에 일반 파일 아닌 항목이 있다: {name}")
        for name in names:
            os.link(
                name, name,
                src_dir_fd=quarantine_fd, dst_dir_fd=original_fd,
                follow_symlinks=False,
            )
            linked.append(name)
    except BaseException:
        for name in linked:
            try:
                if original_fd is not None:
                    os.unlink(name, dir_fd=original_fd)
            except OSError:
                pass
        if original_fd is not None:
            os.close(original_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        try:
            os.rmdir(original, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    # Every child now has an exclusive hardlink at the original name. Cleanup
    # failure may leave duplicate quarantine links, but cannot lose the restore.
    try:
        for name in names:
            os.unlink(name, dir_fd=quarantine_fd)
    except OSError as exc:
        raise LinkError(f"rollback 완료 후 격리 정리 실패: {quarantine}") from exc
    finally:
        os.close(original_fd)
        os.close(quarantine_fd)
    os.rmdir(quarantine, dir_fd=directory_fd)


def _restore_moves(directory_fd: int, moves: list[tuple[str, str]]) -> None:
    failed: list[str] = []
    for original, quarantine in reversed(moves):
        try:
            quarantined = os.stat(
                quarantine, dir_fd=directory_fd, follow_symlinks=False
            )
            if stat.S_ISDIR(quarantined.st_mode):
                _restore_quarantined_directory(
                    directory_fd, quarantine, original
                )
            else:
                os.link(
                    quarantine, original,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.unlink(quarantine, dir_fd=directory_fd)
        except (OSError, LinkError):
            failed.append(quarantine)
    if failed:
        raise LinkError(
            "rollback 실패 — 격리 경로를 보존했다: " + ", ".join(failed)
        )


def _quarantine_one(
    directory_fd: int,
    original: str,
    initial: os.stat_result,
    moves: list[tuple[str, str]],
) -> str:
    quarantine = f".{original}.svc-unlink-{secrets.token_hex(8)}"
    try:
        os.rename(
            original, quarantine,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
    except OSError as exc:
        _restore_moves(directory_fd, moves)
        raise LinkError(f"unlink 격리 실패: {original}") from exc
    try:
        moved = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        _restore_moves(directory_fd, moves + [(original, quarantine)])
        raise LinkError(f"격리된 unlink 대상을 확인할 수 없다: {original}") from exc
    if (moved.st_dev, moved.st_ino, moved.st_mode) != (
        initial.st_dev, initial.st_ino, initial.st_mode
    ):
        current = moves + [(original, quarantine)]
        _restore_moves(directory_fd, current)
        raise LinkError(f"unlink 대상이 검사 중 바뀌었다: {original}")
    moves.append((original, quarantine))
    return quarantine


def _remove_quarantined_directory(
    conf_fd: int,
    quarantine: str,
    directory_identity: tuple[int, int, int],
    expected: dict[str, tuple[int, int, int]],
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dropin_fd = os.open(quarantine, flags, dir_fd=conf_fd)
    try:
        opened = os.fstat(dropin_fd)
        if (opened.st_dev, opened.st_ino, opened.st_mode) != directory_identity:
            raise LinkError(f"격리된 dropin 디렉터리가 바뀌었다: {quarantine}")
        names = set(os.listdir(dropin_fd))
        if names != set(expected):
            raise LinkError(f"격리된 dropin 내용이 바뀌었다: {quarantine}")
        for name, identity in expected.items():
            current = os.stat(name, dir_fd=dropin_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != identity:
                raise LinkError(f"격리된 dropin 파일이 바뀌었다: {quarantine}/{name}")
        for name in expected:
            os.unlink(name, dir_fd=dropin_fd)
        os.fsync(dropin_fd)
    finally:
        os.close(dropin_fd)
    os.rmdir(quarantine, dir_fd=conf_fd)


def unlink(
    name: str,
    config_root: Path,
    *,
    purge_dropins: bool = False,
) -> Unlinked:
    """Remove only the conf.d symlink, even when its target is broken."""
    path, logical = _linked_path(name, config_root)
    del logical
    conf_dir, conf_fd = _open_conf_dir(config_root)
    try:
        try:
            link_stat = os.stat(path.name, dir_fd=conf_fd, follow_symlinks=False)
        except OSError as exc:
            raise LinkError(f"조각 링크를 확인할 수 없다 ({path}): {exc}") from exc
        if not stat.S_ISLNK(link_stat.st_mode):
            raise LinkError(f"심링크가 아니므로 unlink를 거부한다: {path}")

        declared = _declared_names(path)
        dropin_dirs = _candidate_dropin_dirs(path, config_root, declared)
        if purge_dropins and declared is None:
            raise LinkError("조각 선언을 증명할 수 없어 purge 소유 범위가 불명확하다")

        dropins: tuple[Path, ...] = ()
        directory_stats: dict[str, os.stat_result] = {}
        child_stats: dict[str, dict[str, tuple[int, int, int]]] = {}
        for directory in dropin_dirs:
            if directory.parent != conf_dir or not directory.name.endswith(".d"):
                raise LinkError(f"conf.d direct child가 아닌 dropin 경로: {directory}")
            safe_name = directory.name[:-2]
            if _NAME.fullmatch(safe_name) is None:
                if purge_dropins:
                    raise LinkError(f"안전하지 않은 dropin 이름: {directory.name}")
                dropins += (directory,)
                continue
            directory_stat = os.stat(
                directory.name, dir_fd=conf_fd, follow_symlinks=False
            )
            if not stat.S_ISDIR(directory_stat.st_mode):
                if purge_dropins:
                    raise LinkError(f"드롭인 경로가 실제 디렉터리가 아니다: {directory}")
                dropins += (directory,)
                continue
            children = tuple(sorted(directory.iterdir()))
            dropins += children if children else (directory,)
            if purge_dropins:
                unsafe = tuple(
                    child for child in children
                    if child.suffix != ".toml"
                    or not stat.S_ISREG(child.lstat().st_mode)
                )
                if unsafe:
                    raise LinkError(
                        "예상하지 않은 드롭인 항목이 있어 purge를 거부한다: "
                        + ", ".join(str(child) for child in unsafe)
                    )
                directory_stats[directory.name] = directory_stat
                identities = {}
                for child in children:
                    child_stat = child.lstat()
                    identities[child.name] = (
                        child_stat.st_dev, child_stat.st_ino, child_stat.st_mode
                    )
                child_stats[directory.name] = identities

        moves: list[tuple[str, str]] = []
        _quarantine_one(conf_fd, path.name, link_stat, moves)
        if purge_dropins:
            for directory in dropin_dirs:
                _quarantine_one(
                    conf_fd, directory.name, directory_stats[directory.name], moves
                )

        # Every public pathname is now removed as one logical commit. Cleanup
        # only touches verified random quarantine names in this same directory.
        fragment_quarantine = moves[0][1]
        try:
            current_fragment = os.stat(
                fragment_quarantine, dir_fd=conf_fd, follow_symlinks=False
            )
            if (
                current_fragment.st_dev,
                current_fragment.st_ino,
                current_fragment.st_mode,
            ) != (link_stat.st_dev, link_stat.st_ino, link_stat.st_mode):
                raise LinkError(f"격리된 fragment가 바뀌었다: {fragment_quarantine}")
            os.unlink(fragment_quarantine, dir_fd=conf_fd)
            for original, quarantine in moves[1:]:
                original_stat = directory_stats[original]
                _remove_quarantined_directory(
                    conf_fd,
                    quarantine,
                    (original_stat.st_dev, original_stat.st_ino, original_stat.st_mode),
                    child_stats[original],
                )
            os.fsync(conf_fd)
        except (OSError, LinkError) as exc:
            remaining = tuple(
                quarantine for _, quarantine in moves
                if os.path.lexists(conf_dir / quarantine)
            )
            raise LinkError(
                "unlink commit 후 격리 정리 실패 — 복구 경로: "
                + ", ".join(str(conf_dir / item) for item in remaining)
            ) from exc
        return Unlinked(path, dropins, purge_dropins and bool(dropin_dirs))
    finally:
        os.close(conf_fd)


def listing(config_root: Path) -> tuple[Listing, ...]:
    """List fragment links without loading the configuration graph."""
    rows: list[Listing] = []
    conf_dir = Path(config_root) / "conf.d"
    for path in sorted(conf_dir.glob("*.toml")):
        try:
            path.lstat()
        except OSError:
            continue
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        if not target.is_absolute():
            target = path.parent / target
        broken = not target.exists()
        count: int | None = None
        if not broken:
            try:
                raw = tomllib.loads(target.read_text())
                entries = raw.get("service", []) if isinstance(raw, dict) else []
                count = len(entries) if isinstance(entries, list) else None
            except (OSError, tomllib.TOMLDecodeError):
                count = None
        rows.append(Listing(
            re.sub(r"^\d\d-", "", path.stem), path, target, count, broken
        ))
    return tuple(rows)


@contextmanager
def _dropin_directory(name: str, config_root: Path, *, create: bool):
    """Open an actual direct-child drop-in directory without following links."""
    if _NAME.fullmatch(name) is None:
        raise LinkError(f"잘못된 서비스 이름: {name!r}")
    conf_dir = Path(config_root) / "conf.d"
    try:
        conf_stat = conf_dir.lstat()
    except OSError as exc:
        raise LinkError(f"conf.d를 확인할 수 없다: {conf_dir}") from exc
    if not stat.S_ISDIR(conf_stat.st_mode):
        raise LinkError(f"conf.d가 실제 디렉터리가 아니다: {conf_dir}")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        conf_fd = os.open(conf_dir, flags)
    except OSError as exc:
        raise LinkError(f"conf.d를 안전하게 열 수 없다: {conf_dir}") from exc
    directory_name = f"{name}.d"
    created = False
    try:
        opened_conf = os.fstat(conf_fd)
        if (opened_conf.st_dev, opened_conf.st_ino, opened_conf.st_mode) != (
            conf_stat.st_dev, conf_stat.st_ino, conf_stat.st_mode
        ):
            raise LinkError(f"conf.d가 검사 중 바뀌었다: {conf_dir}")
        try:
            directory_stat = os.stat(
                directory_name, dir_fd=conf_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            if not create:
                raise LinkError(f"드롭인 디렉터리가 없다: {name}")
            try:
                os.mkdir(directory_name, 0o755, dir_fd=conf_fd)
                created = True
                directory_stat = os.stat(
                    directory_name, dir_fd=conf_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise LinkError(f"드롭인 디렉터리를 만들 수 없다: {name}") from exc
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise LinkError(f"드롭인 경로가 실제 디렉터리가 아니다: {name}.d")
        try:
            dropin_fd = os.open(directory_name, flags, dir_fd=conf_fd)
        except OSError as exc:
            raise LinkError(f"드롭인 디렉터리를 안전하게 열 수 없다: {name}.d") from exc
        try:
            opened_directory = os.fstat(dropin_fd)
            if (
                opened_directory.st_dev,
                opened_directory.st_ino,
                opened_directory.st_mode,
            ) != (
                directory_stat.st_dev,
                directory_stat.st_ino,
                directory_stat.st_mode,
            ):
                raise LinkError(f"드롭인 디렉터리가 검사 중 바뀌었다: {name}.d")
            try:
                yield conf_dir / directory_name, dropin_fd
            except BaseException:
                if created:
                    try:
                        os.rmdir(directory_name, dir_fd=conf_fd)
                    except OSError:
                        pass
                raise
        finally:
            os.close(dropin_fd)
    finally:
        os.close(conf_fd)


def _read_at(directory_fd: int, filename: str) -> tuple[bytes, os.stat_result]:
    try:
        before = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LinkError(f"예약 파일을 확인할 수 없다: {filename}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise LinkError(f"예약 파일 경로가 일반 파일이 아니다: {filename}")
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise LinkError(f"예약 파일을 안전하게 열 수 없다: {filename}") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise LinkError(f"예약 파일이 검사 중 바뀌었다: {filename}")
        chunks = []
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), opened
    finally:
        os.close(fd)


def _publish_exclusive(directory_fd: int, filename: str, payload: bytes) -> None:
    """Publish complete bytes without ever replacing an existing pathname."""
    temporary = f".{filename}.svc-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
    except OSError as exc:
        raise LinkError(f"임시 파일을 만들 수 없다: {filename}") from exc
    try:
        try:
            os.fchmod(fd, 0o644)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise LinkError(f"임시 파일 쓰기가 중단됐다: {filename}")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise LinkError(f"foreign 파일이 먼저 생겨 publish를 거부한다: {filename}") from exc
        os.fsync(directory_fd)
    except LinkError:
        raise
    except OSError as exc:
        raise LinkError(f"파일을 안전하게 publish할 수 없다: {filename}") from exc
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _restore_regular_quarantine(
    directory_fd: int,
    quarantine: str,
    filename: str,
) -> None:
    try:
        os.link(
            quarantine, filename,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(quarantine, dir_fd=directory_fd)
    except OSError as exc:
        raise LinkError(f"예약 파일을 격리에 보존했다: {quarantine}") from exc


def _unlink_exact(directory_fd: int, filename: str, payload: bytes) -> None:
    current, initial = _read_at(directory_fd, filename)
    if current != payload:
        raise LinkError(f"예약 파일이 canonical 형식이 아니다: {filename}")
    quarantine = f".{filename}.svc-unmask-{secrets.token_hex(8)}"
    try:
        os.rename(
            filename, quarantine,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LinkError(f"예약 파일을 격리할 수 없다: {filename}") from exc
    try:
        moved = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        _restore_regular_quarantine(directory_fd, quarantine, filename)
        raise LinkError(f"격리된 예약 파일을 확인할 수 없다: {filename}") from exc
    if (moved.st_dev, moved.st_ino, moved.st_mode) != (
        initial.st_dev, initial.st_ino, initial.st_mode
    ):
        _restore_regular_quarantine(directory_fd, quarantine, filename)
        raise LinkError(f"예약 파일이 검사 중 바뀌었다: {filename}")
    try:
        final = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        _restore_regular_quarantine(directory_fd, quarantine, filename)
        raise LinkError(f"격리된 예약 파일을 재확인할 수 없다: {filename}") from exc
    if (final.st_dev, final.st_ino, final.st_mode) != (
        initial.st_dev, initial.st_ino, initial.st_mode
    ):
        raise LinkError(f"변경된 예약 파일을 격리에 보존했다: {quarantine}")
    os.unlink(quarantine, dir_fd=directory_fd)


def mask(name: str, config_root: Path) -> Path:
    if _NAME.fullmatch(name) is None:
        raise LinkError(f"잘못된 서비스 이름: {name!r}")
    merged = confd.load(config_root)
    if merged.by_name(name) is None:
        raise LinkError(f"그런 서비스가 없다: {name}")
    with _dropin_directory(name, config_root, create=True) as (directory, fd):
        path = directory / "90-mask.toml"
        try:
            current, _ = _read_at(fd, path.name)
        except FileNotFoundError:
            _publish_exclusive(fd, path.name, MASK_CONTENT)
            return path
        if current not in (MASK_CONTENT, LEGACY_MASK_CONTENT):
            raise LinkError(f"예약 파일이 canonical mask가 아니다: {path}")
        return path


def unmask(name: str, config_root: Path) -> Path:
    if _NAME.fullmatch(name) is None:
        raise LinkError(f"잘못된 서비스 이름: {name!r}")
    with _dropin_directory(name, config_root, create=False) as (directory, fd):
        path = directory / "90-mask.toml"
        try:
            current, _ = _read_at(fd, path.name)
            if current not in (MASK_CONTENT, LEGACY_MASK_CONTENT):
                raise LinkError(f"예약 파일이 canonical mask가 아니다: {path}")
            _unlink_exact(fd, path.name, current)
        except FileNotFoundError as exc:
            raise LinkError(f"mask가 없다: {name}") from exc
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return path


def edit(name: str, config_root: Path, editor: str | None = None) -> Path:
    if _NAME.fullmatch(name) is None:
        raise LinkError(f"잘못된 서비스 이름: {name!r}")
    editor_text = editor if editor is not None else os.environ.get("EDITOR", "")
    try:
        command = shlex.split(editor_text)
    except ValueError as exc:
        raise LinkError("$EDITOR 문법이 잘못됐다") from exc
    if not command:
        raise LinkError("$EDITOR가 설정되지 않았다")
    if shutil.which(command[0]) is None:
        raise LinkError("$EDITOR 실행 파일을 찾을 수 없다")
    merged = confd.load(config_root)
    if merged.by_name(name) is None:
        raise LinkError(f"그런 서비스가 없다: {name}")
    with _dropin_directory(name, config_root, create=True) as (directory, fd):
        path = directory / "70-local.toml"
        try:
            _read_at(fd, path.name)
        except FileNotFoundError:
            _publish_exclusive(
                fd, path.name, LOCAL_TEMPLATE.format(name=name).encode()
            )
    try:
        result = subprocess.run([*command, str(path)], check=False)
    except OSError as exc:
        raise LinkError(f"$EDITOR를 실행할 수 없다: {exc}") from exc
    if result.returncode != 0:
        raise LinkError(f"$EDITOR가 실패했다 (exit {result.returncode}): {path}")
    return path


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "# unset"
    if isinstance(value, tuple):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return str(value)


def cat(merged: model.MergedModel, config_root: Path, name: str) -> str:
    """Render effective values from the already-merged model, never raw files."""
    service = merged.by_name(name)
    if service is None:
        raise LinkError(f"그런 서비스가 없다: {name}")
    root = Path(config_root)
    fields = (
        ("name", "name", service.name),
        ("label", "label", service.label),
        ("port", "port", service.port),
        ("group", "group", service.group),
        ("managed", "managed", service.managed),
        ("exec", "exec_argv", service.exec_argv),
        ("depends_on", "depends_on", service.depends_on),
        ("mem_budget", "mem_budget", service.mem_budget),
        ("env", "env", service.env),
        ("working_directory", "working_directory", service.working_directory),
    )
    lines = [f"# {root / 'macosctl.toml'} [defaults]",
             f"working_directory = {_value(merged.defaults.working_directory)}"]
    for config_field, model_field, value in fields:
        if value is None:
            continue
        provenance = tuple(
            item for item in service.sources if item.field == model_field
        )
        history = tuple(item.location or item.source for item in provenance)
        winner = history[-1] if history else service.location_of("name")
        if winner is None:
            continue
        winner_source = provenance[-1].source if provenance else service.source_of("name")
        if model_field == "working_directory" and winner_source == "macosctl.toml":
            continue
        fragment = root / "conf.d" / str(winner_source)
        dropin = root / "conf.d" / f"{name}.d" / str(winner_source)
        base_location = service.location_of("name")
        if winner_source == "macosctl.toml":
            location = root / "macosctl.toml"
        elif provenance and provenance[-1].location is not None:
            location = Path(provenance[-1].location)
        elif winner == base_location:
            location = fragment
        else:
            location = dropin
        lines.append(f"# {location}")
        if len(history) > 1:
            replaced = ", ".join(f"{source}:{config_field}" for source in history[:-1])
            lines.append(f"# overrides {replaced}")
        if config_field == "env":
            rendered = "{ " + ", ".join(
                f"{json.dumps(key)} = {_value(item)}" for key, item in value
            ) + " }"
        else:
            rendered = _value(value)
        lines.append(f"{config_field} = {rendered}")
    return "\n".join(lines) + "\n"
