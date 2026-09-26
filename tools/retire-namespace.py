#!/usr/bin/env python3
"""Retire an explicitly authorized legacy svc namespace after cutover gates pass."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path


class RetirementError(Exception):
    """A legacy artifact cannot be retired safely."""


def _validate_root(root: Path) -> Path:
    if not root.is_absolute() or ".." in root.parts:
        raise RetirementError("--root는 .. 없는 절대 경로여야 한다")
    try:
        root_info = root.lstat()
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RetirementError(f"retirement root를 확정할 수 없다: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise RetirementError(f"retirement root가 실제 디렉터리가 아니다: {root}")
    current = Path(canonical.anchor)
    for part in canonical.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise RetirementError(f"retirement root가 없다: {canonical}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RetirementError(f"retirement root parent가 심링크다: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise RetirementError(
                f"retirement root parent가 디렉터리가 아니다: {current}"
            )
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
            raise RetirementError(f"legacy parent가 심링크다: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise RetirementError(f"legacy parent가 디렉터리가 아니다: {current}")


def _validate_live_alias(root: Path, name: str, expected_target: str) -> None:
    alias = root / name
    try:
        info = alias.lstat()
        target = os.readlink(alias)
    except OSError as exc:
        raise RetirementError(f"macOS live alias를 확인할 수 없다: {alias}") from exc
    if not stat.S_ISLNK(info.st_mode) or target != expected_target:
        raise RetirementError(
            f"macOS live alias가 표준 target이 아니다: {alias} -> {target}"
        )


def _validate_directory(path: Path, description: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RetirementError(f"{description}가 없다: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RetirementError(f"{description}가 실제 디렉터리가 아니다: {path}")


def _validate_cli(path: Path, expected_target: str) -> None:
    if not os.path.lexists(path):
        raise RetirementError(f"old cli가 없다: {path}")
    try:
        observed = path.lstat()
        target = os.readlink(path)
        confirmed = path.lstat()
    except OSError as exc:
        raise RetirementError(f"foreign old cli: {path}") from exc
    if (
        not stat.S_ISLNK(observed.st_mode)
        or not os.path.samestat(observed, confirmed)
        or target != expected_target
    ):
        raise RetirementError(f"foreign old cli: {path}")


def _read_validated_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    description: str,
) -> bytes:
    try:
        observed = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RetirementError(f"foreign old {description}: {path}") from exc
    try:
        acquired = os.fstat(fd)
        if (
            not os.path.samestat(observed, acquired)
            or not stat.S_ISREG(acquired.st_mode)
            or acquired.st_nlink != 1
            or acquired.st_uid != expected_uid
            or acquired.st_gid != expected_gid
            or stat.S_IMODE(acquired.st_mode) != expected_mode
        ):
            raise RetirementError(f"foreign old {description}: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def retire(
    root: Path,
    *,
    expected_cli_target: str,
    service_user: str,
    dry_run: bool = False,
    live: bool = False,
) -> tuple[Path, Path]:
    root = _validate_root(Path(root))
    if not expected_cli_target.startswith("/") or any(
        character in expected_cli_target for character in ("\r", "\n")
    ):
        raise RetirementError("--expected-cli-target은 개행 없는 절대 경로여야 한다")
    if not service_user or any(character in service_user for character in ("\r", "\n")):
        raise RetirementError("--service-user가 잘못됐다")

    if live:
        _validate_live_alias(root, "etc", "private/etc")
        _validate_live_alias(root, "var", "private/var")
        old_config = root / "private" / "etc" / "svc"
        old_state = root / "private" / "var" / "db" / "svc"
        old_sudoers = root / "private" / "etc" / "sudoers.d" / "svcctl"
        expected_uid = 0
        expected_gid = 0
        parent_paths = (
            Path("private/etc/svc"),
            Path("private/var/db/svc"),
            Path("private/etc/sudoers.d"),
            Path("usr/local/bin"),
            Path("usr/local/sbin"),
        )
    else:
        old_config = root / "etc" / "svc"
        old_state = root / "var" / "db" / "svc"
        old_sudoers = root / "etc" / "sudoers.d" / "svcctl"
        expected_uid = os.geteuid()
        expected_gid = os.getegid()
        parent_paths = (
            Path("etc/svc"),
            Path("var/db/svc"),
            Path("etc/sudoers.d"),
            Path("usr/local/bin"),
            Path("usr/local/sbin"),
        )
    for relative in parent_paths:
        _secure_parent_chain(root, relative)

    old_cli = root / "usr" / "local" / "bin" / "svc"
    old_helper = root / "usr" / "local" / "sbin" / "svcctl"
    _validate_directory(old_config, "old config")
    _validate_directory(old_state, "old state")
    _validate_cli(old_cli, expected_cli_target)
    _read_validated_file(
        old_helper,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_mode=0o755,
        description="helper",
    )
    sudoers = _read_validated_file(
        old_sudoers,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_mode=0o440,
        description="sudoers",
    )
    expected_sudoers = (
        f"{service_user} ALL=(root) NOPASSWD: /usr/local/sbin/svcctl\n".encode()
    )
    if sudoers != expected_sudoers:
        raise RetirementError(f"foreign old sudoers: {old_sudoers}")

    if dry_run:
        return old_config, old_state
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RetirementError("이 Python의 rmtree는 symlink 공격을 방어하지 않는다")

    for path in (old_cli, old_sudoers, old_helper):
        try:
            path.unlink()
        except OSError as exc:
            raise RetirementError(f"legacy integration 제거 실패: {path}: {exc}") from exc
    for path in (old_config, old_state):
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise RetirementError(f"legacy namespace 제거 실패: {path}: {exc}") from exc
    return old_config, old_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expected-cli-target", required=True)
    parser.add_argument("--service-user", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.root == Path("/"):
            if not args.live:
                raise RetirementError("root / retirement에는 --live가 필요하다")
            if os.geteuid() != 0:
                raise RetirementError("live namespace retirement에는 root가 필요하다")
            if os.environ.get("MACOSCTL_INSTALLER_MIGRATION") != "1":
                raise RetirementError(
                    "live namespace retirement은 install.sh --migrate-namespace로만 실행한다"
                )
        elif args.live:
            raise RetirementError("--live는 root / 에만 쓸 수 있다")
        old_config, old_state = retire(
            args.root,
            expected_cli_target=args.expected_cli_target,
            service_user=args.service_user,
            dry_run=args.dry_run,
            live=args.live,
        )
    except (RetirementError, OSError) as exc:
        print(f"namespace retirement 오류: {exc}", file=sys.stderr)
        return 2
    prefix = "dry-run " if args.dry_run else ""
    print(f"{prefix}retire {old_config}")
    print(f"{prefix}retire {old_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
