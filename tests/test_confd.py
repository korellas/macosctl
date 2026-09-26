"""conf.d two-layer merge loader tests (D4, D6)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import confd, model  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402


DEFAULTS = '''\
schema = 1
[defaults]
working_directory = "/srv/default"
log_dir = "/tmp/logs"
throttle_seconds = 10
path = "/usr/bin:/bin"
log_rotate_interval_seconds = 900
log_max_mb = 20
log_keep = 5
'''


def _root(tmp: str) -> Path:
    root = Path(tmp)
    (root / "conf.d").mkdir()
    (root / "macosctl.toml").write_text(DEFAULTS)
    return root


def _fragment(name: str, *, label: str | None = None, port: int = 4000,
              extra: str = "") -> str:
    return f'''\
schema = 1
[[service]]
name = "{name}"
label = "{label or f'com.korellas.{name}'}"
port = {port}
group = "infra"
exec = ["/bin/echo", "{name}"]
depends_on = []
{extra}
'''


def test_service_log_dir_overrides_machine_default():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("demo", extra='log_dir = "/var/log/demo"')
        )

        service = confd.load(root).by_name("demo")

        assert service is not None
        assert service.log_dir == "/var/log/demo"


def test_same_name_across_fragments_is_fatal_and_names_both_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(_fragment("litellm"))
        (root / "conf.d" / "30-b.toml").write_text(_fragment("litellm", port=4001))

        try:
            confd.load(root)
        except model.MergeError as exc:
            assert "30-a.toml" in exc.detail
            assert "30-b.toml" in exc.detail
        else:
            raise AssertionError("duplicate name was not fatal")


def test_dropins_apply_in_lexicographic_order():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(_fragment("demo"))
        dropins = root / "conf.d" / "demo.d"
        dropins.mkdir()
        (dropins / "80-port.toml").write_text("schema = 1\nport = 8000\n")
        (dropins / "70-port.toml").write_text("schema = 1\nport = 7000\n")

        service = confd.load(root).by_name("demo")
        assert service is not None
        assert service.port == 8000
        assert service.source_of("port") == "80-port.toml"


def test_dropin_cannot_change_name_or_label():
    for field, value in (("name", '"renamed"'), ("label", '"com.other"')):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            (root / "conf.d" / "30-a.toml").write_text(_fragment("demo"))
            dropins = root / "conf.d" / "demo.d"
            dropins.mkdir()
            (dropins / "70-local.toml").write_text(
                f"schema = 1\n{field} = {value}\n"
            )
            try:
                confd.load(root)
            except model.MergeError as exc:
                assert field in exc.detail and "70-local.toml" in exc.detail
            else:
                raise AssertionError(f"drop-in changed immutable {field}")


def test_all_fields_replace_including_lists_and_working_directory_is_per_service():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        fragment = _fragment(
            "demo",
            extra='working_directory = "/srv/project"\n'
                  'exec = ["/first", "old"]\n'
                  'depends_on = ["one", "two"]',
        ).replace('exec = ["/bin/echo", "demo"]\n', "").replace(
            "depends_on = []\n", ""
        )
        (root / "conf.d" / "30-a.toml").write_text(fragment)
        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
            'schema = 1\nexec = ["/second"]\ndepends_on = ["three"]\n'
            'working_directory = "/srv/local"\n'
        )

        service = confd.load(root).by_name("demo")
        assert service is not None
        assert service.exec_argv == ("/second",)
        assert service.depends_on == ("three",)
        assert service.working_directory == "/srv/local"


def test_missing_service_working_directory_uses_machine_default():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(_fragment("demo"))
        service = confd.load(root).by_name("demo")
        assert service is not None
        assert service.working_directory == "/srv/default"
        assert service.source_of("working_directory") == "macosctl.toml"


def test_env_merges_by_key():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("demo", extra='env = { A = "one", B = "old" }')
        )
        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-env.toml").write_text(
            'schema = 1\nenv = { B = "new", C = "three" }\n'
        )
        service = confd.load(root).by_name("demo")
        assert service is not None
        assert dict(service.env) == {"A": "one", "B": "new", "C": "three"}


def test_unset_removes_env_key_and_whole_optional_field():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment(
                "demo",
                extra='env = { A = "one", B = "two" }\n'
                      'depends_on = ["database"]\nmem_budget = "2GB"',
            ).replace("depends_on = []\n", "")
        )
        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-unset.toml").write_text(
            'schema = 1\nunset = ["env.A", "depends_on", "mem_budget"]\n'
        )
        service = confd.load(root).by_name("demo")
        assert service is not None
        assert service.env == (("B", "two"),)
        assert service.depends_on == ()
        assert service.mem_budget is None
        assert service.source_of("depends_on") == "70-unset.toml"


def test_process_type_defaults_to_background_and_dropin_can_override():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(_fragment("demo"))
        default_service = confd.load(root).by_name("demo")
        assert default_service is not None
        assert default_service.process_type == "Background"

        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
            'schema = 1\nprocess_type = "Standard"\n'
        )
        overridden = confd.load(root).by_name("demo")
        assert overridden is not None
        assert overridden.process_type == "Standard"
        assert overridden.source_of("process_type") == "70-local.toml"


def test_orphan_dropin_is_a_warning_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        orphan = root / "conf.d" / "missing.d"
        orphan.mkdir()
        (orphan / "90-mask.toml").write_text("schema = 1\nmanaged = false\n")
        merged = confd.load(root)
        assert merged.services == ()
        assert len(merged.warnings) == 1
        assert "missing" in merged.warnings[0]
        assert "orphan" in merged.warnings[0]


def test_unreadable_fragment_is_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        broken = root / "conf.d" / "30-broken.toml"
        os.symlink(root / "does-not-exist.toml", broken)
        try:
            confd.load(root)
        except confd.FragmentUnreadable as exc:
            assert "30-broken.toml" in str(exc)
        else:
            raise AssertionError("broken symlink silently retired its services")


def test_unknown_schema_is_fatal_in_every_layer():
    for layer in ("defaults", "fragment", "dropin"):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            if layer == "defaults":
                (root / "macosctl.toml").write_text(DEFAULTS.replace("schema = 1", "schema = 2"))
            else:
                (root / "conf.d" / "30-a.toml").write_text(
                    _fragment("demo").replace("schema = 1", "schema = 2" if layer == "fragment" else "schema = 1")
                )
                if layer == "dropin":
                    (root / "conf.d" / "demo.d").mkdir()
                    (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
                        "schema = 2\nport = 1\n"
                    )
            try:
                confd.load(root)
            except model.MergeError as exc:
                assert "schema" in exc.detail
            else:
                raise AssertionError(f"unknown schema accepted in {layer}")


def test_service_order_follows_fragment_then_file_order():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-b.toml").write_text(
            _fragment("third") + _fragment("fourth", port=4001).replace(
                "schema = 1\n", "", 1
            )
        )
        (root / "conf.d" / "20-a.toml").write_text(
            _fragment("first", port=3000) + _fragment("second", port=3001).replace(
                "schema = 1\n", "", 1
            )
        )
        assert tuple(s.name for s in confd.load(root).services) == (
            "first", "second", "third", "fourth",
        )


def test_provenance_records_every_overridden_field():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("demo", extra='env = { A = "one" }')
        )
        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
            'schema = 1\nport = 7000\nenv = { A = "two" }\n'
        )
        service = confd.load(root).by_name("demo")
        assert service is not None
        port_sources = [p.source for p in service.sources if p.field == "port"]
        env_sources = [p.source for p in service.sources if p.field == "env"]
        assert port_sources == ["30-a.toml", "70-local.toml"]
        assert env_sources == ["30-a.toml", "70-local.toml"]


def test_project_managed_false_is_active_but_dropin_managed_false_is_masked():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("project-off", port=4000, extra="managed = false")
            + _fragment("masked", port=4001).replace("schema = 1\n", "", 1)
        )
        (root / "conf.d" / "masked.d").mkdir()
        (root / "conf.d" / "masked.d" / "90-mask.toml").write_text(
            "schema = 1\nmanaged = false\n"
        )
        merged = confd.load(root)
        project_off = merged.by_name("project-off")
        masked = merged.by_name("masked")
        assert project_off is not None and project_off.lifecycle == "active"
        assert masked is not None and masked.lifecycle == "masked"


def test_user_is_forbidden_outside_a_service_declaration():
    for layer in ("top-level", "dropin", "scaffold"):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            payload = _fragment("demo")
            if layer == "top-level":
                payload = payload.replace(
                    "schema = 1\n", 'schema = 1\nuser = "attacker"\n', 1
                )
            elif layer == "scaffold":
                payload += '\n[scaffold]\nuser = "attacker"\n'
            (root / "conf.d" / "30-a.toml").write_text(payload)
            if layer == "dropin":
                (root / "conf.d" / "demo.d").mkdir()
                (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
                    'schema = 1\nuser = "attacker"\n'
                )
            try:
                confd.load(root)
            except model.MergeError as exc:
                assert "user" in exc.detail
                assert "policy.json" in exc.detail
                assert "service_user" in exc.detail
                assert "유일한 정본" in exc.detail
            else:
                raise AssertionError(f"user accepted in {layer}")


def test_service_declaration_preserves_requested_user_for_policy_binding():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("render", extra='user = "_render"')
        )

        service = confd.load(root).by_name("render")

        assert service is not None
        assert service.user == "_render"
        assert service.source_of("user") == "30-a.toml"


def test_service_declaration_rejects_malformed_user():
    for requested_user in ('""', '"Bad User"', "501"):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            (root / "conf.d" / "30-a.toml").write_text(
                _fragment("render", extra=f"user = {requested_user}")
            )

            try:
                confd.load(root)
            except model.MergeError as exc:
                assert "render" in exc.detail and "user" in exc.detail, exc.detail
            else:
                raise AssertionError(f"malformed service user accepted: {requested_user}")


def test_user_in_svc_toml_is_rejected_with_a_policy_specific_message():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "macosctl.toml").write_text(
            DEFAULTS.replace('[defaults]\n', '[defaults]\nuser = "example"\n')
        )
        try:
            confd.load(root)
        except model.MergeError as exc:
            assert "policy.json" in exc.detail
            assert "service_user" in exc.detail
        else:
            raise AssertionError("macosctl.toml의 user를 거부하지 않았다")


def test_top_level_user_in_svc_toml_gets_the_policy_specific_message_first():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "macosctl.toml").write_text(
            DEFAULTS.replace("schema = 1\n", 'schema = 1\nuser = "example"\n')
        )
        try:
            confd.load(root)
        except model.MergeError as exc:
            assert "policy.json" in exc.detail
            assert "service_user" in exc.detail
            assert "허용되지 않은 필드" not in exc.detail
        else:
            raise AssertionError("top-level user가 generic 검사보다 먼저 거부되지 않았다")


def test_defaults_accepts_every_key_the_live_manifest_uses():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "conf.d").mkdir()
        source = (
            REPO / "tests" / "fixtures" / "drift-synthetic" / "services.toml"
        ).read_text()
        head, marker, tail = source.partition("[[service]]")
        assert marker
        head = "\n".join(
            line for line in head.splitlines() if not line.lstrip().startswith("user ")
        ) + "\n"
        (root / "macosctl.toml").write_text("schema = 1\n" + head)
        (root / "conf.d" / "30-ai.toml").write_text(
            "schema = 1\n" + marker + tail
        )

        merged = confd.load(root)
        assert merged.defaults.log_max_mb == 20
        assert merged.defaults.log_keep == 5


def test_unknown_defaults_key_is_rejected_instead_of_silently_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "macosctl.toml").write_text(DEFAULTS + "mem_budgt = 99\n")
        try:
            confd.load(root)
        except model.MergeError as exc:
            assert "mem_budgt" in exc.detail
        else:
            raise AssertionError("defaults typo was silently ignored")


def test_schema_unset_and_working_directory_are_allowed_only_where_defined():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("demo", extra='working_directory = "/srv/project"')
        )
        (root / "conf.d" / "demo.d").mkdir()
        (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
            'schema = 1\nunset = ["working_directory"]\n'
        )
        service = confd.load(root).by_name("demo")
        assert service is not None
        assert service.working_directory == "/srv/default"

    for misplaced in ("schema = 1", 'unset = ["depends_on"]'):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            (root / "conf.d" / "30-a.toml").write_text(
                _fragment("demo", extra=misplaced)
            )
            try:
                confd.load(root)
            except model.MergeError as exc:
                assert misplaced.split()[0] in exc.detail
            else:
                raise AssertionError(f"service accepted misplaced {misplaced}")


def test_typo_fields_are_rejected_in_service_and_dropin():
    for layer in ("service", "dropin"):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            extra = "working_directry = \"/bad\"" if layer == "service" else ""
            (root / "conf.d" / "30-a.toml").write_text(
                _fragment("demo", extra=extra)
            )
            if layer == "dropin":
                (root / "conf.d" / "demo.d").mkdir()
                (root / "conf.d" / "demo.d" / "70-local.toml").write_text(
                    'schema = 1\nworking_directry = "/bad"\n'
                )
            try:
                confd.load(root)
            except model.MergeError as exc:
                assert "working_directry" in exc.detail
            else:
                raise AssertionError(f"{layer} typo was silently ignored")


def test_fragment_accepts_optional_scaffold_metadata_without_merging_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(
            _fragment("demo") + '\n[scaffold]\nlauncher_dir = "/srv/project"\n'
        )
        assert confd.load(root).by_name("demo") is not None


def _run_svc(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "macosctl"),
            "--config-root",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_rejects_missing_config_root_with_exit_2():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing"
        result = _run_svc(missing, "apply", "--dry-run")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "설정" in result.stderr and "macosctl.toml" in result.stderr
    assert "Traceback" not in result.stderr
    assert len(result.stderr.splitlines()) == 1


def test_cli_rejects_unreadable_fragment_without_traceback():
    commands = (
        ("apply", "--dry-run"),
        ("cat", "demo"),
        ("status",),
        ("doctor",),
        ("manifest", "--json"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        os.symlink(
            root / "missing-project.toml",
            root / "conf.d" / "30-broken.toml",
        )
        results = tuple(_run_svc(root, *command) for command in commands)

    for command, result in zip(commands, results, strict=True):
        assert result.returncode == 2, (command, result.stderr)
        assert result.stdout == "", command
        assert "설정" in result.stderr and "30-broken.toml" in result.stderr
        assert "Traceback" not in result.stderr, command
        assert len(result.stderr.splitlines()) == 1, (command, result.stderr)


def test_cli_rejects_merge_error_without_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "macosctl.toml").write_text(DEFAULTS.replace("schema = 1", "schema = 2"))
        result = _run_svc(root, "status")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "설정" in result.stderr and "MergeError" in result.stderr
    assert "Traceback" not in result.stderr
    assert len(result.stderr.splitlines()) == 1


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
