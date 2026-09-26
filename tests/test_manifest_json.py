"""D14 JSON manifest contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import manifest, model, policy, render  # noqa: E402


def _model_with(**overrides) -> model.MergedModel:
    fields = dict(
        name="demo",
        label="com.korellas.demo",
        port=9999,
        group="test",
        managed=True,
        exec_argv=("/bin/echo", "secret argument"),
        depends_on=(),
        mem_budget=None,
        env=(("TOKEN", "secret"),),
        working_directory="/srv/demo",
        lifecycle="active",
        sources=(model.Provenance("name", "30-demo.toml"),),
    )
    fields.update(overrides)
    defaults = manifest.Defaults(
        user="example",
        working_directory="/srv",
        log_dir="/tmp/services",
        throttle_seconds=10,
        path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )
    return model.MergedModel(
        defaults=defaults,
        services=(model.MergedService(**fields),),
        warnings=(),
    )


def _config_root(tmp: str, *, service_extra: str = "") -> Path:
    root = Path(tmp) / "config"
    (root / "conf.d").mkdir(parents=True)
    root.chmod(0o755)
    (root / "macosctl.toml").write_text(
        'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
        'log_dir = "/tmp/services"\nthrottle_seconds = 10\n'
    )
    (root / "conf.d" / "30-demo.toml").write_text(
        'schema = 1\n[[service]]\nname = "demo"\n'
        'label = "com.korellas.demo"\nport = 9999\ngroup = "test"\n'
        'exec = ["/bin/echo", "secret"]\ndepends_on = []\n'
        + service_extra
    )
    policy_path = root / "policy.json"
    policy_path.write_text(
        '{"schema":1,"label_prefix":"com.korellas.",'
        '"label_exceptions":{},"service_user":"example",'
        '"groups":["test"]}'
    )
    policy_path.chmod(0o644)
    return root


def _run_manifest(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "macosctl"),
            "--config-root",
            str(root),
            "manifest",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_mem_budget_is_bytes_not_string():
    merged = _model_with(mem_budget="44GB")

    out = json.loads(render.manifest_json(merged))

    assert out["services"][0]["mem_budget"] == 44 * 1024 ** 3


def test_mem_budget_keeps_the_existing_parse_size_inputs():
    for text, expected in (
        ("1.5GB", 1_610_612_736),
        ("512 MiB", 512 * 1024 ** 2),
        ("2048", 2048),
    ):
        service = json.loads(
            render.manifest_json(_model_with(mem_budget=text))
        )["services"][0]
        assert service["mem_budget"] == expected


def test_exec_and_env_are_not_exposed():
    out = json.loads(render.manifest_json(_model_with()))

    assert set(out) == {"schema", "defaults", "services"}
    assert set(out["defaults"]) == {"log_dir", "throttle_seconds"}
    assert set(out["services"][0]) == {
        "name",
        "label",
        "port",
        "group",
        "managed",
        "lifecycle",
        "mem_budget",
        "depends_on",
        "working_directory",
        "source",
    }
    assert "secret argument" not in render.manifest_json(_model_with())
    assert "TOKEN" not in render.manifest_json(_model_with())


def test_managed_and_lifecycle_are_orthogonal():
    unmanaged = json.loads(
        render.manifest_json(_model_with(managed=False, lifecycle="active"))
    )["services"][0]
    assert unmanaged["managed"] is False
    assert unmanaged["lifecycle"] == "active"

    try:
        render.manifest_json(_model_with(managed=True, lifecycle="masked"))
    except ValueError as exc:
        assert "managed" in str(exc) and "masked" in str(exc)
    else:
        raise AssertionError("managed:true + masked was rendered")


def test_lifecycle_is_the_closed_contract_enum():
    try:
        render.manifest_json(_model_with(managed=False, lifecycle="paused"))
    except ValueError as exc:
        assert "lifecycle" in str(exc)
    else:
        raise AssertionError("unknown lifecycle was rendered")


def test_nullable_fields_match_the_contract():
    service = json.loads(render.manifest_json(_model_with(mem_budget=None)))[
        "services"
    ][0]
    assert service["mem_budget"] is None
    assert all(
        value is not None for key, value in service.items() if key != "mem_budget"
    )

    try:
        render.manifest_json(_model_with(sources=()))
    except ValueError as exc:
        assert "source" in str(exc)
    else:
        raise AssertionError("nullable source was rendered")


def _assert_render_refuses(merged: model.MergedModel, field: str) -> None:
    try:
        render.manifest_json(merged)
    except ValueError as exc:
        assert field in str(exc), str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{field} raised {type(exc).__name__}, not ValueError: {exc}"
        ) from exc
    else:
        raise AssertionError(f"invalid {field} type was rendered")


def test_every_json_field_enforces_its_exact_contract_type():
    base = _model_with()
    for field, value in (
        ("name", None),
        ("label", 7),
        ("group", None),
        ("working_directory", False),
        ("port", True),
        ("managed", 1),
        ("lifecycle", 1),
        ("mem_budget", 44),
        ("depends_on", ("database", 7)),
        ("source", 30),
    ):
        if field == "source":
            merged = _model_with(
                sources=(model.Provenance("name", value),)
            )
        else:
            merged = _model_with(**{field: value})
        _assert_render_refuses(merged, field)

    _assert_render_refuses(
        replace(base, defaults=replace(base.defaults, log_dir=None)),
        "log_dir",
    )
    _assert_render_refuses(
        replace(base, defaults=replace(base.defaults, throttle_seconds=True)),
        "throttle_seconds",
    )


def test_mem_budget_errors_are_value_errors_and_fit_u64():
    maximum = 2 ** 64 - 1
    out = json.loads(
        render.manifest_json(_model_with(mem_budget=str(maximum)))
    )
    assert out["services"][0]["mem_budget"] == maximum

    for invalid in (44, str(2 ** 64), "-1", "nonsense"):
        _assert_render_refuses(_model_with(mem_budget=invalid), "mem_budget")


def test_service_order_follows_merge_order():
    base = _model_with()
    first = replace(
        base.services[0],
        name="first",
        label="com.korellas.first",
        sources=(model.Provenance("name", "20-first.toml"),),
    )
    second = replace(
        base.services[0],
        name="second",
        label="com.korellas.second",
        port=10000,
        sources=(model.Provenance("name", "30-second.toml"),),
    )
    merged = model.MergedModel(base.defaults, (second, first), ())

    out = json.loads(render.manifest_json(merged))

    assert [service["name"] for service in out["services"]] == ["second", "first"]


def test_output_is_deterministic_and_newline_terminated():
    merged = _model_with()

    first = render.manifest_json(merged)
    second = render.manifest_json(merged)

    assert first == second
    assert first.endswith("\n") and not first.endswith("\n\n")
    assert "timestamp" not in first and "generated" not in first


def test_write_uses_atomic_replace_and_0644():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.json"
        path.write_text("old")
        with mock.patch("macosctl.render.os.replace", wraps=os.replace) as replace_call:
            render.write(_model_with(), path)

        source, destination = replace_call.call_args.args
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        assert path.read_text() == render.manifest_json(_model_with())
        assert path.stat().st_mode & 0o777 == 0o644


def test_default_manifest_write_sets_root_wheel_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        temporary = Path(tmp) / "payload"
        fd = os.open(temporary, os.O_CREAT | os.O_RDWR, 0o600)
        with (
            mock.patch(
                "macosctl.render.tempfile.mkstemp",
                return_value=(fd, str(temporary)),
            ),
            mock.patch("macosctl.render.os.fchown") as fchown,
            mock.patch("macosctl.render.os.replace"),
            mock.patch("macosctl.render.os.unlink"),
        ):
            render.write(_model_with(), render.MANIFEST_PATH)

        fchown.assert_called_once_with(fd, 0, policy.WHEEL_GID)


def test_cli_stdout_is_json_only_and_uses_caller_owned_staging_policy():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_manifest(_config_root(tmp))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["services"][0]["name"] == "demo"
    assert result.stdout.startswith("{") and result.stdout.endswith("\n")
    assert "\x1b" not in result.stdout
    assert result.stderr == ""


def test_cli_accepts_macos_tmp_inherited_wheel_gid_for_staging():
    with tempfile.TemporaryDirectory(prefix="svc-manifest-", dir="/tmp") as tmp:
        root = _config_root(tmp)
        assert root.stat().st_uid == os.getuid()
        assert root.stat().st_gid != os.getgid()
        result = _run_manifest(root)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == 1


def test_cli_validation_fatal_has_exit_2_and_no_stdout():
    with tempfile.TemporaryDirectory() as tmp:
        root = _config_root(tmp)
        fragment = root / "conf.d" / "30-demo.toml"
        fragment.write_text(
            fragment.read_text().replace('depends_on = []', 'depends_on = ["ghost"]')
        )
        result = _run_manifest(root)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "dangling-dependency" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_warnings_go_to_stderr_without_polluting_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = _config_root(tmp)
        orphan = root / "conf.d" / "ghost.d"
        orphan.mkdir()
        (orphan / "90-mask.toml").write_text("schema = 1\nlifecycle = \"masked\"\n")
        result = _run_manifest(root)

    assert result.returncode == 0
    assert json.loads(result.stdout)["schema"] == 1
    assert "orphan" in result.stderr and "ghost" in result.stderr


def test_cli_other_error_has_exit_1_and_no_stdout():
    with tempfile.TemporaryDirectory() as tmp:
        root = _config_root(tmp, service_extra='mem_budget = "nonsense"\n')
        result = _run_manifest(root)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "mem_budget" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_wrong_toml_field_type_has_exit_1_and_no_stdout():
    with tempfile.TemporaryDirectory() as tmp:
        root = _config_root(tmp, service_extra="working_directory = true\n")
        result = _run_manifest(root)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "working_directory" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_malformed_user_collections_are_clean_exit_1_errors():
    cases = (
        (None, None, 'mem_budget = 10\n'),
        ('depends_on = []', 'depends_on = 42', ''),
        ('exec = ["/bin/echo", "secret"]', 'exec = 42', ''),
        (None, None, 'env = 42\n'),
    )
    for old, new, extra in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = _config_root(tmp, service_extra=extra)
            if old is not None:
                fragment = root / "conf.d" / "30-demo.toml"
                fragment.write_text(fragment.read_text().replace(old, new))
            result = _run_manifest(root)

        assert result.returncode == 1, (old, extra, result.stderr)
        assert result.stdout == "", (old, extra, result.stdout)
        assert result.stderr.strip(), (old, extra)
        assert "Traceback" not in result.stderr, (old, extra, result.stderr)


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
