"""Task 12 staging config builder tests (D11-2)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "stage-config.py"
sys.path.insert(0, str(REPO))

from macosctl import confd, manifest, plist, policy  # noqa: E402


LEGACY = '''\
# legacy source must stay byte-identical
[defaults]
user = "tester"
working_directory = "/srv/app"
log_dir = "/tmp/services"
throttle_seconds = 10
log_max_mb = 20
log_keep = 5
log_rotate_interval_seconds = 900
path = "/usr/bin:/bin"

[[service]]
name = "demo"
label = "com.korellas.demo"
port = 4321
group = "infra"
exec = ["/bin/echo", "ok"]
env = { TOKEN = "value" }
depends_on = []

[[service]]
name = "webtop"
label = "com.webtop"
port = 7890
group = "dashboard"
exec = ["/bin/echo", "web"]
depends_on = ["demo"]
'''


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("stage_config_under_test", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_stage_builds_complete_config_without_mutating_legacy_source():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        before = source.read_bytes()
        out = base / "macosctl-staging"

        result = tool.stage(source, out)

        assert result == out.resolve()
        assert source.read_bytes() == before
        assert sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file()) == [
            "conf.d/30-ai.toml", "macosctl.toml", "policy.json",
        ]
        defaults = tomllib.loads((out / "macosctl.toml").read_text())
        fragment = tomllib.loads((out / "conf.d" / "30-ai.toml").read_text())
        assert defaults["schema"] == fragment["schema"] == 1
        assert "user" not in defaults["defaults"]
        assert "defaults" not in fragment
        assert "user" not in fragment
        assert [entry["name"] for entry in fragment["service"]] == ["demo", "webtop"]
        assert json.loads((out / "policy.json").read_text()) == {
            "schema": 1,
            "label_prefix": "com.korellas.",
            "label_exceptions": {"webtop": "com.webtop"},
            "service_user": "tester",
            "groups": ["infra", "dashboard"],
        }
        assert stat.S_IMODE(out.stat().st_mode) == 0o755
        assert stat.S_IMODE((out / "conf.d").stat().st_mode) == 0o755
        assert stat.S_IMODE((out / "policy.json").stat().st_mode) == 0o644
        assert stat.S_IMODE((out / "macosctl.toml").stat().st_mode) == 0o644


def test_staged_model_preserves_every_legacy_service_plist_byte():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        out = base / "svc-staging"
        tool.stage(source, out)

        merged = confd.load(out)
        staged_policy = policy.read_staging(out / "policy.json")
        merged = policy.bind(merged, staged_policy)
        legacy_defaults = manifest.load_defaults(source)
        legacy_services = {service.name: service for service in manifest.load(source)}

        assert {service.name for service in merged.services} == set(legacy_services)
        for service in merged.services:
            assert plist.build(service, merged.defaults) == plist.build(
                legacy_services[service.name], legacy_defaults
            ), service.name
            assert service.source_of("name") == "30-ai.toml"


def test_stage_replaces_only_the_exact_staging_directory():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        out = base / "svc-staging"
        out.mkdir()
        (out / "OLD").write_text("replace")
        sibling = base / "OWNER-SIBLING"
        sibling.write_text("keep")

        tool.stage(source, out)

        assert not (out / "OLD").exists()
        assert sibling.read_text() == "keep"
        assert source.read_text() == LEGACY


def test_stage_refuses_output_symlink_without_touching_its_target():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        owner = base / "OWNER"
        owner.mkdir()
        (owner / "data").write_text("keep")
        out = base / "svc-staging"
        os.symlink(owner, out)

        try:
            tool.stage(source, out)
        except tool.StageError:
            pass
        else:
            raise AssertionError("output symlink를 따라 staging target 밖에 썼다")

        assert out.is_symlink()
        assert (owner / "data").read_text() == "keep"
        assert sorted(path.name for path in owner.iterdir()) == ["data"]


def test_failed_build_preserves_previous_staging_tree():
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text("not valid toml = [")
        out = base / "svc-staging"
        out.mkdir()
        (out / "OWNER").write_text("keep")

        try:
            tool.stage(source, out)
        except tool.StageError:
            pass
        else:
            raise AssertionError("invalid source를 staging했다")

        assert (out / "OWNER").read_text() == "keep"


def test_output_swap_race_restores_foreign_entry_without_deleting_it():
    """lstat 뒤 들어온 foreign symlink는 backup으로 오인해 삭제하거나 숨기지 않는다."""
    tool = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        out = base / "svc-staging"
        out.mkdir()
        (out / "OLD").write_text("old")
        saved = base / "actor-saved"
        owner = base / "OWNER"
        owner.mkdir()
        (owner / "data").write_text("keep")
        original_rename = tool.os.rename
        effective_out = out.parent.resolve() / out.name
        raced = False

        def racing_rename(src, dst):
            nonlocal raced
            if not raced and Path(src) == effective_out:
                raced = True
                original_rename(effective_out, saved)
                os.symlink(owner, effective_out)
            return original_rename(src, dst)

        tool.os.rename = racing_rename
        try:
            try:
                tool.stage(source, out)
            except tool.StageError:
                pass
            else:
                raise AssertionError("output swap race를 허용했다")
        finally:
            tool.os.rename = original_rename

        assert raced
        assert out.is_symlink() and out.resolve() == owner.resolve()
        assert (owner / "data").read_text() == "keep"
        assert (saved / "OLD").read_text() == "old"
        assert not list(base.glob(".macosctl-staging.backup.*")), \
            "foreign symlink를 hidden backup으로 남겼다"


def test_cli_accepts_an_independent_output_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "services.toml"
        source.write_text(LEGACY)
        before = source.read_bytes()
        out = base / "comparison-a"

        result = subprocess.run(
            [sys.executable, str(TOOL), "--out", str(out), "--manifest", str(source)],
            capture_output=True, text=True, check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(out.resolve())
        assert (out / "policy.json").is_file()
        assert (out / "macosctl.toml").is_file()
        assert (out / "conf.d" / "30-ai.toml").is_file()
        assert source.read_bytes() == before


def test_cli_reports_self_loop_manifest_without_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "services.toml"
        os.symlink(source.name, source)

        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--out",
                "/tmp/macosctl-staging",
                "--manifest",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 2
        assert "stage-config 오류" in result.stderr
        assert "Traceback" not in result.stderr


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
    sys.exit(1 if failures else 0)
