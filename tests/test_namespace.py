"""Public/source namespace contracts for the macosctl rename."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import apply, confd, inventory, policy, state  # noqa: E402


def test_only_macosctl_source_and_executable_names_exist():
    assert (REPO / "macosctl").is_dir()
    assert not (REPO / "svc").exists()
    assert (REPO / "bin" / "macosctl").is_file()
    assert not (REPO / "bin" / "svc").exists()
    assert (REPO / "sbin" / "macosctl-helper").is_file()
    assert not (REPO / "sbin" / "svcctl").exists()


def test_python_sources_import_only_the_macosctl_package():
    roots = (REPO / "macosctl", REPO / "bin", REPO / "tools", REPO / "tests")
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__):
                continue
            text = path.read_text()
            assert "from svc" not in text, path
            assert "import svc" not in text, path


def test_runtime_paths_use_only_the_macosctl_namespace():
    assert confd.CONFIG_ROOT == Path("/etc/macosctl")
    assert confd.DEFAULTS_BASENAME == "macosctl.toml"
    assert policy.POLICY_PATH == Path("/etc/macosctl/policy.json")
    assert inventory.DEFAULT_PATH == Path("/var/db/macosctl/inventory")
    assert state.DEFAULT_PATH == Path("/var/db/macosctl/state")
    assert state.DEFAULT_LOCK == Path("/var/db/macosctl/lock")
    assert apply.APPLY_LOCK == Path("/var/db/macosctl/apply.lock")

    cli = runpy.run_path(str(REPO / "bin" / "macosctl"))
    assert cli["SVCCTL"] == "/usr/local/sbin/macosctl-helper"

    helper = runpy.run_path(str(REPO / "sbin" / "macosctl-helper"))
    assert helper["POLICY"] == Path("/etc/macosctl/policy.json")
    assert helper["INVENTORY"] == Path("/var/db/macosctl/inventory")
    assert helper["STATE"] == Path("/var/db/macosctl/state")
    assert helper["LOCK"] == Path("/var/db/macosctl/lock")
    assert helper["AUDIT_LOG"] == Path("/var/log/macosctl-helper.log")


def test_production_sources_have_no_old_runtime_paths():
    paths = (
        *sorted((REPO / "macosctl").glob("*.py")),
        REPO / "bin" / "macosctl",
        REPO / "sbin" / "macosctl-helper",
        REPO / "tools" / "stage-config.py",
    )
    for path in paths:
        text = path.read_text()
        assert "/etc/svc" not in text, path
        assert "/var/db/svc" not in text, path
        assert "/var/log/svcctl.log" not in text, path


def test_current_operator_docs_use_the_macosctl_namespace():
    current_docs = (
        REPO / "README.md",
        REPO / "docs" / "services.md",
    )
    forbidden = (
        "/etc/svc", "/var/db/svc", "/tmp/svc-staging",
        "/usr/local/bin/svc", "/usr/local/sbin/svcctl",
        "/etc/sudoers.d/svcctl", "/var/log/svcctl.log",
        "`svc ", "sudo svc ", "bin/svc", "sbin/svcctl",
    )
    for path in current_docs:
        text = path.read_text()
        for residue in forbidden:
            assert residue not in text, (path, residue)



if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
