"""Safe old -> new namespace migration tests."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "migrate-namespace.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("migrate_namespace", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _old_tree(root: Path) -> None:
    config = root / "etc" / "svc"
    state = root / "var" / "db" / "svc"
    (config / "conf.d" / "demo.d").mkdir(parents=True)
    state.mkdir(parents=True)
    (config / "policy.json").write_text('{"schema": 1}\n')
    (config / "svc.toml").write_text("schema = 1\n")
    (config / "conf.d" / "30-demo.toml").write_text("schema = 1\n")
    (config / "conf.d" / "demo.d" / "70-local.toml").write_text("schema = 1\n")
    os.symlink("/projects/demo/service.toml", config / "conf.d" / "40-project.toml")
    (state / "inventory").write_bytes(b'{"version":2,"labels":{}}\n')
    (state / "state").write_bytes(b'{"version":1,"services":{}}\n')
    (state / "lock").write_bytes(b"")


def _old_macos_tree(root: Path) -> None:
    (root / "private").mkdir()
    os.symlink("private/etc", root / "etc")
    os.symlink("private/var", root / "var")
    _old_tree(root / "private")


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    if not root.exists():
        return {}
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            result[rel] = ("link", os.readlink(path), stat.S_IMODE(info.st_mode))
        elif stat.S_ISREG(info.st_mode):
            result[rel] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
        else:
            result[rel] = ("dir", b"", stat.S_IMODE(info.st_mode))
    return result


def test_migration_copies_config_and_state_without_touching_old_namespace():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        old_config = root / "etc" / "svc"
        old_state = root / "var" / "db" / "svc"
        before_config = _snapshot(old_config)
        before_state = _snapshot(old_state)

        tool.migrate(root)

        new_config = root / "etc" / "macosctl"
        new_state = root / "var" / "db" / "macosctl"
        assert _snapshot(old_config) == before_config
        assert _snapshot(old_state) == before_state
        expected_config = {
            ("macosctl.toml" if key == "svc.toml" else key): value
            for key, value in before_config.items()
        }
        assert _snapshot(new_config) == expected_config
        assert _snapshot(new_state) == before_state


def test_dry_run_is_zero_write_and_reports_order():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        before = _snapshot(root)
        result = subprocess.run(
            [sys.executable, str(TOOL), "--root", str(root), "--dry-run"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert _snapshot(root) == before
        assert result.stdout.index("/etc/macosctl") < result.stdout.index("/var/db/macosctl")


def test_foreign_new_destination_is_rejected_without_partial_publish():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        foreign = root / "etc" / "macosctl"
        foreign.mkdir(parents=True)
        (foreign / "OWNER").write_text("keep")
        before = _snapshot(root)
        try:
            tool.migrate(root)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("foreign destination was accepted")
        assert _snapshot(root) == before


def test_publish_renames_relative_to_parent_directory_descriptors():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        original_rename = tool.os.rename
        calls = []

        def checked_rename(src, dst, *args, **kwargs):
            assert isinstance(kwargs.get("src_dir_fd"), int)
            assert isinstance(kwargs.get("dst_dir_fd"), int)
            calls.append((src, dst))
            return original_rename(src, dst, *args, **kwargs)

        tool.os.rename = checked_rename
        try:
            tool.migrate(root)
        finally:
            tool.os.rename = original_rename

        assert len(calls) == 2


def test_second_publish_failure_rolls_back_only_new_namespaces():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        before_config = _snapshot(root / "etc" / "svc")
        before_state = _snapshot(root / "var" / "db" / "svc")
        original_rename = tool.os.rename
        calls = 0

        def fail_second(src, dst, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second publish failure")
            return original_rename(src, dst, *args, **kwargs)

        tool.os.rename = fail_second
        try:
            try:
                tool.migrate(root)
            except tool.MigrationError:
                pass
            else:
                raise AssertionError("second publish failure was ignored")
        finally:
            tool.os.rename = original_rename

        assert not (root / "etc" / "macosctl").exists()
        assert not (root / "var" / "db" / "macosctl").exists()
        assert _snapshot(root / "etc" / "svc") == before_config
        assert _snapshot(root / "var" / "db" / "svc") == before_state


def test_identical_new_trees_are_idempotent():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        tool.migrate(root)
        before = _snapshot(root)
        tool.migrate(root)
        assert _snapshot(root) == before


def test_symlink_parent_and_unsafe_entry_are_rejected():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        outside = base / "outside"
        outside.mkdir()
        root.mkdir()
        os.symlink(outside, root / "etc")
        try:
            tool.migrate(root)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("symlink parent was accepted")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        fifo = root / "etc" / "svc" / "conf.d" / "unsafe"
        os.mkfifo(fifo)
        try:
            tool.migrate(root)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("unsafe entry was accepted")


def test_scan_rejects_regular_file_replaced_by_symlink_after_observation():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        source = root / "etc" / "svc"
        victim = source / "conf.d" / "30-demo.toml"
        sentinel = root / "root-only-sentinel"
        sentinel.write_bytes(b"ROOT_SECRET")
        swapped = False

        def swap(relative: str) -> None:
            nonlocal swapped
            if relative == "conf.d/30-demo.toml" and not swapped:
                swapped = True
                victim.unlink()
                os.symlink(sentinel, victim)

        try:
            tool._scan(source, config=True, _after_observe=swap)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("scanner followed a replacement symlink")
        assert swapped


def test_scan_rejects_directory_replaced_by_symlink_after_observation():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_tree(root)
        source = root / "etc" / "svc"
        victim = source / "conf.d" / "demo.d"
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"OUTSIDE_SECRET")
        swapped = False

        def swap(relative: str) -> None:
            nonlocal swapped
            if relative == "conf.d/demo.d" and not swapped:
                swapped = True
                victim.rename(source / "conf.d" / "demo.old")
                os.symlink(outside, victim)

        try:
            tool._scan(source, config=True, _after_observe=swap)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("scanner followed a replacement directory symlink")
        assert swapped


def test_authorized_live_cli_forwards_live_mode_to_migrate():
    tool = _load_tool()
    calls = []
    original_geteuid = tool.os.geteuid
    original_migrate = tool.migrate
    previous = tool.os.environ.get("MACOSCTL_INSTALLER_MIGRATION")

    def fake_migrate(root, *, dry_run=False, live=False):
        calls.append((root, dry_run, live))
        return Path("/etc/macosctl"), Path("/var/db/macosctl")

    try:
        tool.os.geteuid = lambda: 0
        tool.os.environ["MACOSCTL_INSTALLER_MIGRATION"] = "1"
        tool.migrate = fake_migrate
        assert tool.main(["--root", "/", "--live", "--dry-run"]) == 0
    finally:
        tool.os.geteuid = original_geteuid
        tool.migrate = original_migrate
        if previous is None:
            tool.os.environ.pop("MACOSCTL_INSTALLER_MIGRATION", None)
        else:
            tool.os.environ["MACOSCTL_INSTALLER_MIGRATION"] = previous

    assert calls == [(Path("/"), True, True)]


def test_live_mode_accepts_only_the_standard_macos_aliases_and_reports_logical_paths():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_macos_tree(root)
        canonical = root.resolve()
        old_config = root / "private" / "etc" / "svc"
        old_state = root / "private" / "var" / "db" / "svc"
        before_config = _snapshot(old_config)
        before_state = _snapshot(old_state)

        config, state = tool.migrate(root, live=True)

        assert config == canonical / "etc" / "macosctl"
        assert state == canonical / "var" / "db" / "macosctl"
        assert _snapshot(old_config) == before_config
        assert _snapshot(old_state) == before_state
        assert _snapshot(root / "private" / "etc" / "macosctl") == {
            ("macosctl.toml" if key == "svc.toml" else key): value
            for key, value in before_config.items()
        }
        assert _snapshot(root / "private" / "var" / "db" / "macosctl") == before_state


def test_live_mode_dry_run_is_zero_write_with_standard_macos_aliases():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_macos_tree(root)
        canonical = root.resolve()
        before = _snapshot(root)

        config, state = tool.migrate(root, dry_run=True, live=True)

        assert config == canonical / "etc" / "macosctl"
        assert state == canonical / "var" / "db" / "macosctl"
        assert _snapshot(root) == before


def test_generic_mode_still_rejects_the_standard_macos_aliases():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _old_macos_tree(root)
        before = _snapshot(root)

        try:
            tool.migrate(root, dry_run=True)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("generic migration accepted symlink parents")

        assert _snapshot(root) == before


def test_live_mode_rejects_nonstandard_alias_targets_without_writing():
    tool = _load_tool()
    for alias, target in (("etc", "private/owner-etc"), ("var", "private/owner-var")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _old_macos_tree(root)
            (root / alias).unlink()
            os.symlink(target, root / alias)
            before = _snapshot(root)

            try:
                tool.migrate(root, dry_run=True, live=True)
            except tool.MigrationError:
                pass
            else:
                raise AssertionError(f"live migration accepted {alias} -> {target}")

            assert _snapshot(root) == before


def test_live_mode_rejects_a_symlink_in_the_canonical_parent_chain():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        outside = base / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "private").mkdir()
        os.symlink(outside, root / "private" / "etc")
        (root / "private" / "var" / "db").mkdir(parents=True)
        os.symlink("private/etc", root / "etc")
        os.symlink("private/var", root / "var")
        before = _snapshot(base)

        try:
            tool.migrate(root, dry_run=True, live=True)
        except tool.MigrationError:
            pass
        else:
            raise AssertionError("live migration followed a canonical-parent symlink")

        assert _snapshot(base) == before


def test_live_root_requires_explicit_installer_authority():
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", "/", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--live" in result.stderr
    assert "Traceback" not in result.stderr


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
