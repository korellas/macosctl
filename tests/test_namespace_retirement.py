"""One-shot legacy namespace retirement tests."""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "retire-namespace.py"
EXPECTED_TARGET = "/example/pre-rename/svc/bin/svc"


def _old_tree(root: Path) -> None:
    config = root / "etc" / "svc"
    state = root / "var" / "db" / "svc"
    config.mkdir(parents=True)
    state.mkdir(parents=True)
    (config / "policy.json").write_text('{"schema":1}\n')
    (config / "svc.toml").write_text("schema = 1\n")
    (state / "inventory").write_text('{"version":2,"labels":{}}\n')

    cli = root / "usr" / "local" / "bin" / "svc"
    cli.parent.mkdir(parents=True)
    os.symlink(EXPECTED_TARGET, cli)

    helper = root / "usr" / "local" / "sbin" / "svcctl"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"legacy helper\n")
    helper.chmod(0o755)

    sudoers = root / "etc" / "sudoers.d" / "svcctl"
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text(
        f"{getpass.getuser()} ALL=(root) NOPASSWD: /usr/local/sbin/svcctl\n"
    )
    sudoers.chmod(0o440)


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    result: dict[str, tuple[str, bytes | str, int]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = str(path.relative_to(root))
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result[relative] = ("link", os.readlink(path), mode)
        elif stat.S_ISREG(info.st_mode):
            result[relative] = ("file", path.read_bytes(), mode)
        else:
            result[relative] = ("dir", b"", mode)
    return result


def _run(root: Path, target: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--root",
            str(root.resolve()),
            "--expected-cli-target",
            target,
            "--service-user",
            getpass.getuser(),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_accepts_exact_dangling_target_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        before = _snapshot(root)

        result = _run(root, EXPECTED_TARGET, "--dry-run")

        assert result.returncode == 0, result.stderr
        assert _snapshot(root) == before


def test_different_target_is_rejected_before_any_removal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        before = _snapshot(root)

        result = _run(root, "/different/svc")

        assert result.returncode == 2
        assert "foreign old cli" in result.stderr
        assert _snapshot(root) == before


def test_regular_cli_is_rejected_before_any_removal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        cli = root / "usr" / "local" / "bin" / "svc"
        cli.unlink()
        cli.write_text("foreign\n")
        before = _snapshot(root)

        result = _run(root, EXPECTED_TARGET)

        assert result.returncode == 2
        assert "foreign old cli" in result.stderr
        assert _snapshot(root) == before


def test_missing_cli_is_rejected_before_any_removal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        (root / "usr" / "local" / "bin" / "svc").unlink()
        before = _snapshot(root)

        result = _run(root, EXPECTED_TARGET)

        assert result.returncode == 2
        assert "old cli가 없다" in result.stderr
        assert _snapshot(root) == before


def test_helper_mode_mismatch_is_rejected_before_any_removal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        (root / "usr" / "local" / "sbin" / "svcctl").chmod(0o700)
        before = _snapshot(root)

        result = _run(root, EXPECTED_TARGET)

        assert result.returncode == 2
        assert "foreign old helper" in result.stderr
        assert _snapshot(root) == before


def test_sudoers_content_mismatch_is_rejected_before_any_removal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        sudoers = root / "etc" / "sudoers.d" / "svcctl"
        sudoers.chmod(0o600)
        sudoers.write_text("foreign\n")
        sudoers.chmod(0o440)
        before = _snapshot(root)

        result = _run(root, EXPECTED_TARGET)

        assert result.returncode == 2
        assert "foreign old sudoers" in result.stderr
        assert _snapshot(root) == before


def test_success_removes_only_the_five_validated_old_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        marker = root / "OWNER-DATA"
        marker.write_text("keep")

        result = _run(root, EXPECTED_TARGET)

        assert result.returncode == 0, result.stderr
        for relative in (
            "etc/svc",
            "var/db/svc",
            "usr/local/bin/svc",
            "usr/local/sbin/svcctl",
            "etc/sudoers.d/svcctl",
        ):
            assert not os.path.lexists(root / relative)
        assert marker.read_text() == "keep"


def test_live_mode_is_rejected_for_a_staging_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)

        result = _run(root, EXPECTED_TARGET, "--live", "--dry-run")

        assert result.returncode == 2
        assert "--live는 root / 에만" in result.stderr


def test_helper_source_contains_no_historical_machine_target():
    source = TOOL.read_text()
    assert "/Users/example/project/bin/svc" not in source
    assert "/Users/example/git/svc/bin/svc" not in source


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            try:
                value()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
