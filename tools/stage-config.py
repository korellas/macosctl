#!/usr/bin/env python3
"""Build the exact D11-2 staging config without modifying the legacy source."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from macosctl import confd, policy  # noqa: E402

class StageError(Exception):
    """The legacy source or staging destination is unsafe/invalid."""


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise StageError(f"macosctl.toml로 옮길 수 없는 defaults 값: {type(value).__name__}")


def _artifacts(source: Path) -> tuple[bytes, bytes, bytes]:
    try:
        raw = source.read_text()
        data = tomllib.loads(raw)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise StageError(f"legacy manifest를 읽을 수 없다 ({source}): {exc}") from exc

    if "schema" in data and data["schema"] != 1:
        raise StageError("legacy manifest schema는 없거나 1이어야 한다")
    defaults = data.get("defaults")
    services = data.get("service")
    if not isinstance(defaults, dict):
        raise StageError("legacy manifest에 [defaults]가 없다")
    if not isinstance(services, list) or not services:
        raise StageError("legacy manifest에 [[service]]가 없다")
    service_user = defaults.get("user")
    if (
        not isinstance(service_user, str)
        or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", service_user) is None
    ):
        raise StageError("legacy defaults.user가 잘못됐다")

    allowed_defaults = {
        "user",
        "working_directory",
        "log_dir",
        "throttle_seconds",
        "path",
        "log_rotate_interval_seconds",
        "log_max_mb",
        "log_keep",
    }
    unknown_defaults = sorted(set(defaults) - allowed_defaults)
    if unknown_defaults:
        raise StageError(
            "모르는 legacy defaults 키: " + ", ".join(unknown_defaults)
        )

    defaults_lines = ["schema = 1", "", "[defaults]"]
    for key, value in defaults.items():
        if key != "user":
            defaults_lines.append(f"{key} = {_toml_value(value)}")
    defaults_payload = ("\n".join(defaults_lines) + "\n").encode()

    # Preserve the service declarations byte-for-byte.  Only the defaults header and
    # its scalar assignments are removed; comments remain harmless TOML comments.
    fragment_lines: list[str] = []
    in_defaults = False
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[defaults\](?:\s*#.*)?", stripped):
            in_defaults = True
            continue
        if in_defaults and stripped.startswith("["):
            in_defaults = False
        if in_defaults and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", stripped):
            continue
        fragment_lines.append(line)
    fragment = "".join(fragment_lines)
    if "schema" not in data:
        fragment = "schema = 1\n" + fragment
    fragment_payload = fragment.encode()

    prefix = "com.korellas."
    exceptions: dict[str, str] = {}
    groups: list[str] = []
    for entry in services:
        if not isinstance(entry, dict):
            raise StageError("legacy service가 TOML table이 아니다")
        name = entry.get("name")
        label = entry.get("label")
        group = entry.get("group")
        if not all(isinstance(value, str) for value in (name, label, group)):
            raise StageError("legacy service name/label/group이 문자열이 아니다")
        if label != prefix + name:
            exceptions[name] = label
        if group not in groups:
            groups.append(group)
    policy_payload = (
        json.dumps(
            {
                "schema": 1,
                "label_prefix": prefix,
                "label_exceptions": exceptions,
                "service_user": service_user,
                "groups": groups,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    return policy_payload, defaults_payload, fragment_payload


def _write(path: Path, payload: bytes, mode: int) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_tree(root: Path) -> None:
    try:
        merged = confd.load(root)
        staged_policy = policy.read_staging(root / "policy.json")
        policy.bind(merged, staged_policy)
    except (
        OSError,
        TypeError,
        ValueError,
        confd.FragmentUnreadable,
        confd.model.MergeError,
        policy.PolicyRefused,
    ) as exc:
        raise StageError(f"생성한 staging config 검증 실패: {exc}") from exc


def _canonical_output(out: Path) -> Path:
    out = Path(out)
    if not out.is_absolute():
        raise StageError("staging output은 절대 경로여야 한다")
    if ".." in out.parts:
        raise StageError("staging output에 .. 를 쓸 수 없다")
    try:
        parent = out.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StageError(f"staging parent를 확정할 수 없다: {out.parent}") from exc
    if not parent.is_dir():
        raise StageError(f"staging parent가 디렉터리가 아니다: {parent}")
    canonical = parent / out.name
    if canonical == parent or canonical == Path("/"):
        raise StageError("너무 넓은 staging output은 허용하지 않는다")
    if os.path.lexists(canonical) and canonical.is_symlink():
        raise StageError(f"staging output symlink는 허용하지 않는다: {canonical}")
    return canonical


def stage(manifest_path: Path, out_path: Path) -> Path:
    """Build and validate one disposable staging directory."""
    try:
        source = Path(manifest_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StageError(
            f"legacy manifest 경로를 확정할 수 없다 ({manifest_path}): {exc}"
        ) from exc
    out = _canonical_output(Path(out_path))
    try:
        source.relative_to(out)
    except ValueError:
        pass
    else:
        raise StageError("legacy source가 staging output 안에 있어 교체할 수 없다")

    policy_payload, defaults_payload, fragment_payload = _artifacts(source)
    temporary = Path(tempfile.mkdtemp(prefix=".macosctl-staging.", dir=out.parent))
    backup: Path | None = None
    backup_verified = False
    published = False
    try:
        temporary.chmod(0o755)
        conf = temporary / "conf.d"
        conf.mkdir(mode=0o755)
        _write(temporary / "policy.json", policy_payload, 0o644)
        _write(temporary / "macosctl.toml", defaults_payload, 0o644)
        _write(conf / "30-ai.toml", fragment_payload, 0o644)
        _validate_tree(temporary)

        if os.path.lexists(out):
            before = out.lstat()
            if not stat.S_ISDIR(before.st_mode):
                raise StageError(f"기존 staging output이 실제 디렉터리가 아니다: {out}")
            reserved = Path(tempfile.mkdtemp(prefix=".macosctl-staging.backup.", dir=out.parent))
            reserved.rmdir()
            os.rename(out, reserved)
            backup = reserved
            after = reserved.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                # The entry moved was not the directory we inspected.  It is a
                # foreign race winner, never an old staging tree to purge.
                if not os.path.lexists(out):
                    os.rename(reserved, out)
                    backup = None
                raise StageError("staging output이 교체 중 바뀌었다")
            backup_verified = True

        try:
            os.rename(temporary, out)
            published = True
        except OSError:
            if backup is not None and not os.path.lexists(out):
                os.rename(backup, out)
                backup = None
            raise

        if backup is not None:
            shutil.rmtree(backup)
            backup = None
        return out
    except StageError:
        raise
    except OSError as exc:
        raise StageError(f"staging config 기록 실패: {exc}") from exc
    finally:
        if not published and temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        if backup is not None and os.path.lexists(backup):
            if not os.path.lexists(out):
                os.rename(backup, out)
                backup = None
            else:
                kind = "기존 staging" if backup_verified else "foreign entry"
                # A verified old tree is recoverable; an unverified entry must
                # never reach rmtree.  Both stay named when restoration would
                # clobber a new race winner at ``out``.
                print(f"경고: {kind} backup 보존: {backup}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        result = stage(args.manifest, args.out)
    except (OSError, StageError) as exc:
        print(f"stage-config 오류: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
