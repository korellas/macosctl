"""svc reconcile 계층 validate 단위 테스트."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import inventory  # noqa: E402
from macosctl import manifest  # noqa: E402
from macosctl import model  # noqa: E402
from macosctl import policy  # noqa: E402
from macosctl import plist as plistgen  # noqa: E402
from macosctl import validate  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402

DAEMON_DIR = Path("/Library/LaunchDaemons")


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


def test_validate_accepts_current_manifest():
    with mock.patch("macosctl.validate._physical_memory_gb", return_value=None):
        assert validate.check(
            manifest.load(MANIFEST), manifest.load_defaults(MANIFEST)
        ) == ()


def test_validate_rejects_label_outside_namespace():
    problems = validate.check((_service(label="com.github.actions.runner"),),
                              manifest.load_defaults(MANIFEST))
    assert any(p.code == "bad-label" for p in problems), problems


def test_validate_allows_webtop_special_case():
    svc = _service(name="webtop", label="com.webtop")
    assert not [p for p in validate.check((svc,), manifest.load_defaults(MANIFEST))
                if p.code == "bad-label"]


def test_validate_rejects_unknown_process_type():
    svc = model.MergedService(
        name="demo", label="com.korellas.demo", port=9999, group="test",
        managed=True, exec_argv=("/bin/echo", "hi"), depends_on=(),
        mem_budget=None, env=(), working_directory="/tmp",
        lifecycle="active", sources=(), process_type="Realtime",
    )
    problems = validate.check((svc,), manifest.load_defaults(MANIFEST))
    assert any(p.code == "bad-process-type" for p in problems), problems


def test_validate_uses_the_loaded_policy_instead_of_a_hard_coded_namespace():
    loaded_policy = policy.Policy(
        label_prefix="org.example.",
        label_exceptions={},
        service_user="example",
        groups=("test",),
    )
    service = _service(label="org.example.demo")

    problems = validate.check(
        (service,), manifest.load_defaults(MANIFEST), label_policy=loaded_policy
    )

    assert not [problem for problem in problems if problem.code == "bad-label"]


def test_validate_rejects_root_user():
    defaults = manifest.load_defaults(MANIFEST)
    root_defaults = manifest.Defaults(**{**defaults.__dict__, "user": "root"})
    problems = validate.check((_service(),), root_defaults)
    assert any(p.code == "root-user" for p in problems), problems


def test_validate_rejects_per_service_root_user():
    svc = model.MergedService(
        name="demo", label="com.korellas.demo", port=9999, group="test",
        managed=True, exec_argv=("/bin/echo", "hi"), depends_on=(),
        mem_budget=None, env=(), working_directory="/tmp",
        lifecycle="active", sources=(), user="root",
    )

    problems = validate.check((svc,), manifest.load_defaults(MANIFEST))

    assert any(
        problem.code == "root-user" and problem.service == "demo"
        for problem in problems
    ), problems


def test_validate_rejects_duplicate_port_and_label():
    dupes = (_service(), _service(name="demo2"))
    codes = {p.code for p in validate.check(dupes, manifest.load_defaults(MANIFEST))}
    assert "duplicate-port" in codes
    assert "duplicate-label" in codes


def test_validate_rejects_dangling_dependency():
    svc = _service(depends_on=("ghost",))
    problems = validate.check((svc,), manifest.load_defaults(MANIFEST))
    assert any(p.code == "dangling-dependency" for p in problems)


def test_validate_rejects_missing_executable():
    svc = _service(exec_argv=("/nonexistent/launcher.sh", "foreground"))
    problems = validate.check((svc,), manifest.load_defaults(MANIFEST))
    assert any(p.code == "exec-missing" for p in problems)


def test_validate_rejects_port_env_mismatch():
    """port와 env.PORT가 이중 선언이라 어긋날 수 있다 (2R Fable M-2)."""
    svc = _service(port=8001, env=(("PORT", "8002"),))
    problems = validate.check((svc,), manifest.load_defaults(MANIFEST))
    assert any(p.code == "inconsistent-port" for p in problems)


def test_validate_warns_on_memory_budget_overcommit():
    big = tuple(
        _service(name=f"m{i}", label=f"com.korellas.m{i}", port=9000 + i,
                 mem_budget="200GB")
        for i in range(3)
    )
    with mock.patch("macosctl.validate._physical_memory_gb", return_value=256):
        problems = validate.check(big, manifest.load_defaults(MANIFEST))
    over = [p for p in problems if p.code == "memory-overcommit"]
    assert over and over[0].fatal is False, "예산 초과는 경고여야 한다 (중단 아님)"


def test_memory_budget_warning_uses_detected_physical_memory():
    services = (
        _service(name="small", label="com.korellas.small", mem_budget="20GB"),
    )
    completed = mock.Mock(stdout="17179869184\n")  # 16 GiB

    with mock.patch("macosctl.validate.subprocess.run", return_value=completed):
        problems = validate.check(services, manifest.load_defaults(MANIFEST))

    assert any(p.code == "memory-overcommit" for p in problems), problems


def test_memory_budget_warning_is_skipped_when_detection_fails():
    services = (
        _service(name="large", label="com.korellas.large", mem_budget="999TB"),
    )

    with mock.patch("macosctl.validate.subprocess.run", side_effect=OSError("no sysctl")):
        problems = validate.check(services, manifest.load_defaults(MANIFEST))

    assert not [p for p in problems if p.code == "memory-overcommit"], problems


def test_validate_separates_fatal_from_warning():
    problems = validate.check((_service(label="com.evil.thing"),),
                              manifest.load_defaults(MANIFEST))
    assert validate.has_fatal(problems) is True
    assert validate.has_fatal(()) is False

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
