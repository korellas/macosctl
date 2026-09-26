"""svc reconcile 계층 plist 단위 테스트."""

import json
import os
import plistlib
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import inventory  # noqa: E402
from macosctl import manifest  # noqa: E402
from macosctl import model  # noqa: E402
from macosctl import plist  # noqa: E402
from macosctl import plist as plistgen  # noqa: E402
from macosctl import validate  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402

PLIST_FIXTURES = REPO / "tests" / "fixtures" / "plist"


def _service(**over):
    base = dict(
        name="demo",
        label="com.korellas.demo",
        port=9999,
        group="test",
        managed=True,
        exec_argv=("/bin/echo", "hi"),
        depends_on=(),
        mem_budget=None,
        env=(),
    )
    base.update(over)
    return manifest.Service(**base)


def test_generated_plist_matches_reference_bytes():
    defaults = manifest.load_defaults(MANIFEST)
    assert plistgen.build(_service(), defaults) == (PLIST_FIXTURES / "service.plist").read_bytes()


def test_log_rotate_plist_matches_reference_bytes():
    generated = plist.build_log_rotate(
        manifest.load_defaults(MANIFEST),
        Path("/opt/macosctl"),
        Path("/etc/macosctl/macosctl.toml"),
    )
    assert generated == (PLIST_FIXTURES / "log-rotate.plist").read_bytes()


def test_plist_contains_keepalive_and_runatload():
    body = plistgen.build(_service(), manifest.load_defaults(MANIFEST))
    assert b"<key>KeepAlive</key>" in body
    assert b"<key>RunAtLoad</key>" in body


def test_same_content_compares_equal_after_normalization():
    defaults = manifest.load_defaults(MANIFEST)
    body = plistgen.build(_service(), defaults)
    with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as fh:
        fh.write(body)
        path = Path(fh.name)
    try:
        assert plistgen.same_as_installed(body, path) is True
        assert plistgen.same_as_installed(b"<plist/>", path) is False
        assert plistgen.same_as_installed(body, Path("/nonexistent.plist")) is False
    finally:
        path.unlink()


def test_service_working_directory_overrides_machine_default():
    service = model.MergedService(
        name="demo",
        label="com.korellas.demo",
        port=9999,
        group="test",
        managed=True,
        exec_argv=("/bin/echo", "hi"),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/srv/project",
        lifecycle="active",
        sources=(),
    )
    defaults = manifest.load_defaults(MANIFEST)
    payload = plistlib.loads(plistgen.build(service, defaults))
    assert payload["WorkingDirectory"] == "/srv/project"


def test_service_log_dir_overrides_machine_default():
    service = model.MergedService(
        name="render",
        label="com.korellas.render",
        port=9999,
        group="test",
        managed=True,
        exec_argv=("/bin/echo", "hi"),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/srv/render",
        lifecycle="active",
        sources=(),
        log_dir="/var/log/render",
    )
    defaults = manifest.load_defaults(MANIFEST)

    payload = plistlib.loads(plistgen.build(service, defaults))

    assert payload["StandardOutPath"] == "/var/log/render/render.out.log"
    assert payload["StandardErrorPath"] == "/var/log/render/render.err.log"


def test_service_user_override_controls_launchd_username_and_home():
    service = model.MergedService(
        name="render",
        label="com.korellas.render",
        port=9999,
        group="test",
        managed=True,
        exec_argv=("/bin/echo", "hi"),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/srv/render",
        lifecycle="active",
        sources=(),
        user="_render",
    )
    defaults = manifest.load_defaults(MANIFEST)

    payload = plistlib.loads(plistgen.build(service, defaults))

    assert payload["UserName"] == "_render"
    assert payload["EnvironmentVariables"]["HOME"] == "/Users/_render"


def test_service_without_override_keeps_global_launchd_user():
    defaults = manifest.load_defaults(MANIFEST)

    payload = plistlib.loads(plistgen.build(_service(), defaults))

    assert payload["UserName"] == defaults.user
    assert payload["EnvironmentVariables"]["HOME"] == f"/Users/{defaults.user}"


def test_log_file_retention_defaults_do_not_change_plist_bytes():
    defaults = manifest.load_defaults(MANIFEST)
    changed = replace(defaults, log_max_mb=999, log_keep=99)
    service = _service()

    assert plistgen.build(service, defaults) == plistgen.build(service, changed)
    config_file = Path("/etc/macosctl/macosctl.toml")
    assert plistgen.build_log_rotate(
        defaults, REPO, config_file
    ) == plistgen.build_log_rotate(
        changed, REPO, config_file
    )


def test_log_rotate_plist_receives_selected_config_file():
    defaults = manifest.load_defaults(MANIFEST)
    config_file = Path("/private/tmp/macosctl-test/macosctl.toml")
    payload = plistlib.loads(
        plistgen.build_log_rotate(defaults, REPO, config_file)
    )

    assert payload["EnvironmentVariables"]["MACOSCTL_CONFIG"] == str(config_file)

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
