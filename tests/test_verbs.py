"""macosctl CLI 동사 계층 — systemctl식 의미론의 사용자 표면.

start/stop은 runtime, enable/disable은 boot policy. 이 파일은 CLI가 그 구분을
help text와 플래그 조합으로 실제로 지키는지, 그리고 --wait이 helper 성공 뒤에
선언 포트 소유권을 기다리는지를 고정한다.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import collect  # noqa: E402

MACOSCTL = REPO / "bin" / "macosctl"


def _write_config(root: Path) -> None:
    root.chmod(0o755)
    (root / "conf.d").mkdir()
    (root / "macosctl.toml").write_text(
        'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
    )
    (root / "conf.d" / "30-demo.toml").write_text(
        "schema = 1\n\n"
        "[[service]]\n"
        'name = "demo"\n'
        'label = "com.korellas.demo"\n'
        "port = 9999\n"
        'group = "infra"\n'
        'exec = ["/bin/echo", "demo"]\n'
        "depends_on = []\n"
    )
    (root / "policy.json").write_text(json.dumps({
        "schema": 1,
        "label_prefix": "com.korellas.",
        "label_exceptions": {"webtop": "com.webtop"},
        "service_user": "example",
        "groups": ["infra", "dashboard"],
    }))
    (root / "policy.json").chmod(0o644)


def _run(*argv, config_root=None):
    args = [sys.executable, str(MACOSCTL)]
    if config_root is not None:
        args += ["--config-root", str(config_root)]
    args += list(argv)
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _help_for(verb: str) -> str:
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{verb} ") or stripped == verb:
            return stripped
    raise AssertionError(f"{verb}가 도움말에 없다:\n{result.stdout}")


def test_help_separates_runtime_verbs_from_boot_policy_verbs():
    """도움말 한 줄이 곧 계약이다 — 어느 축을 바꾸는지가 거기 적혀 있어야 한다."""
    assert "지금 기동" in _help_for("start") and "boot 변경 없음" in _help_for("start")
    assert "지금 정지" in _help_for("stop") and "boot 변경 없음" in _help_for("stop")
    assert "boot 허용" in _help_for("enable") and "--now" in _help_for("enable")
    assert "boot 차단" in _help_for("disable") and "--now" in _help_for("disable")
    assert "inactive" in _help_for("restart") and "boot 보존" in _help_for("restart")


def test_apply_help_states_the_plist_diff_boundary():
    assert "plist 변경만" in _help_for("apply")


def test_apply_help_exposes_repeatable_service_scope():
    result = _run("apply", "--help")
    assert result.returncode == 0, result.stderr
    assert "--service" in result.stdout


def test_restart_rejects_force_together_with_recreate():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        result = _run(
            "restart", "demo", "--force", "--recreate", config_root=root
        )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--force" in result.stderr and "--recreate" in result.stderr


def test_timeout_without_wait_is_a_usage_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        result = _run("start", "demo", "--timeout", "30", config_root=root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--wait" in result.stderr


def test_stop_and_enable_do_not_accept_runtime_recovery_flags():
    """플래그 표면이 곧 의미론이다 — stop에 --recreate가 있으면 축이 섞인다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        for verb, flag in (
            ("stop", "--recreate"), ("stop", "--force"),
            ("enable", "--force"), ("start", "--recreate"),
            ("restart", "--now"),
        ):
            result = _run(verb, "demo", flag, config_root=root)
            assert result.returncode == 2, f"{verb} {flag}: {result.stdout}"


def test_enable_and_disable_accept_now():
    import argparse
    import runpy

    module = runpy.run_path(str(MACOSCTL), run_name="not_main")
    parser_holder = {}

    original_parse = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        parser_holder["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            module["main"]()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original_parse

    parser = parser_holder["parser"]
    actions = {
        action.dest: action for action in parser._subparsers._group_actions
    }
    choices = actions["command"].choices
    for verb in ("enable", "disable"):
        flags = {
            option for action in choices[verb]._actions
            for option in action.option_strings
        }
        assert "--now" in flags, f"{verb}에 --now가 없다: {flags}"
    for verb in ("start", "restart"):
        flags = {
            option for action in choices[verb]._actions
            for option in action.option_strings
        }
        assert {"--wait", "--timeout"} <= flags, f"{verb}: {flags}"
    assert "--recreate" in {
        option for action in choices["restart"]._actions
        for option in action.option_strings
    }


def test_default_wait_timeout_is_five_minutes():
    import runpy

    module = runpy.run_path(str(MACOSCTL), run_name="not_main")
    assert module["DEFAULT_WAIT_TIMEOUT_SECONDS"] == 300


def test_wait_succeeds_only_when_the_job_tree_owns_the_declared_port():
    """포트를 쥔 것이 launchd job의 자손이어야 준비 완료다.

    남의 프로세스가 그 포트를 쥐고 있어도 '떴다'고 보고하면, webtop 장애처럼
    아무도 서빙하지 않는 상태를 성공으로 읽는다.
    """
    import runpy

    module = runpy.run_path(str(MACOSCTL), run_name="not_main")
    ready = module["_port_is_owned"]

    job = collect.Job(label="com.korellas.demo", loaded=True, pid=10)
    owned = collect.SystemState(
        installed_labels=frozenset({"com.korellas.demo"}),
        jobs={"com.korellas.demo": job},
        listeners=(collect.Listener(port=9999, pid=11, command="demo"),),
        ppids={11: 10}, disabled_overrides={},
    )
    foreign = collect.SystemState(
        installed_labels=frozenset({"com.korellas.demo"}),
        jobs={"com.korellas.demo": job},
        listeners=(collect.Listener(port=9999, pid=77, command="rogue"),),
        ppids={77: 1}, disabled_overrides={},
    )
    absent = collect.SystemState(
        installed_labels=frozenset(), jobs={}, listeners=(), ppids={},
        disabled_overrides={},
    )
    assert ready(owned, "com.korellas.demo", 9999) is True
    assert ready(foreign, "com.korellas.demo", 9999) is False
    assert ready(absent, "com.korellas.demo", 9999) is False



# --- --now 합성의 소유자는 CLI다 ---------------------------------------------------
#
# root helper의 권한 경계는 단일 동작 하나(start|stop|restart|enable|disable)다.
# "boot 허용하고 지금 띄운다" 같은 복합 UX를 helper 안에서 합성하면, 그 경계가
# 사용자 편의를 따라 넓어진다. 합성은 비특권 CLI가 하고, helper는 매번 한 동작만
# 인가·실행한다.


def _load_cli():
    """bin/macosctl을 in-process로 올려 함수의 실제 전역을 잡는다.

    runpy는 네임스페이스의 **복사본**을 돌려주므로 반환 dict를 고쳐도 함수가 보는
    전역은 그대로다. 함수의 __globals__를 직접 잡아야 stub이 먹는다.
    """
    import runpy

    module = runpy.run_path(str(MACOSCTL), run_name="not_main")
    return module, module["cmd_verb"].__globals__


class _FakeHelperRuns:
    """sudo/helper 호출을 가로채 argv와 반환코드만 기록한다."""

    def __init__(self, codes):
        self.codes = list(codes)
        self.argvs = []

    def run(self, argv, *args, **kwargs):
        import subprocess as sp

        self.argvs.append(list(argv))
        code = self.codes.pop(0) if self.codes else 0
        return sp.CompletedProcess(argv, code)

    def verbs(self):
        # ["/usr/bin/sudo", "-n", <helper>, <verb>, <name>, *flags]
        return [argv[3] for argv in self.argvs]

    def flags(self):
        return [flag for argv in self.argvs for flag in argv[5:]]


def _invoke_verb(root: Path, command: str, name, *, codes=(0, 0), waits=None, **flags):
    """실제 인자 파싱부터 실행하되 helper와 포트 대기는 가짜로 바꾼다."""
    import contextlib
    import io

    module, cli = _load_cli()
    runs = _FakeHelperRuns(codes)

    fake_helper = root / "fake-macosctl-helper"
    fake_helper.write_text("#!/bin/sh\nexit 0\n")
    fake_helper.chmod(0o755)

    original_run = cli["subprocess"].run
    original_svcctl = cli["SVCCTL"]
    original_argv = sys.argv
    original_wait = cli["_wait_for_port"]
    cli["SVCCTL"] = str(fake_helper)
    cli["subprocess"].run = runs.run

    names = [name] if isinstance(name, str) else name
    sys.argv = [str(MACOSCTL), "--config-root", str(root), command, *names]
    for flag, value in flags.items():
        if value is not False and value is not None:
            sys.argv.append("--" + flag)
            if value is not True:
                sys.argv.append(str(value))
    if waits is not None:
        def wait_for_port(service, timeout):
            waits.append((service.name, timeout))
            return 0
        cli["_wait_for_port"] = wait_for_port
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = module["main"]()
            except SystemExit as exc:
                code = exc.code
    finally:
        cli["subprocess"].run = original_run
        cli["SVCCTL"] = original_svcctl
        cli["_wait_for_port"] = original_wait
        sys.argv = original_argv
    return code, runs, err.getvalue()


def test_enable_now_issues_exactly_two_helper_calls_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, _ = _invoke_verb(root, "enable", "demo", now=True)
    assert code == 0
    assert runs.verbs() == ["enable", "start"], runs.argvs
    assert "--now" not in runs.flags(), runs.argvs
    for argv in runs.argvs:
        assert argv[4] == "demo", argv


def test_disable_now_issues_disable_then_stop():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, _ = _invoke_verb(root, "disable", "demo", now=True)
    assert code == 0
    assert runs.verbs() == ["disable", "stop"], runs.argvs
    assert "--now" not in runs.flags(), runs.argvs


def test_now_never_runs_the_second_step_when_the_first_fails():
    """첫 단계가 실패했으면 둘째는 금지다 — 이루지 못한 전제 위에 쌓지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, _ = _invoke_verb(root, "enable", "demo", now=True, codes=(3, 0))
    assert code == 3
    assert runs.verbs() == ["enable"], runs.argvs


def test_now_partial_success_is_reported_nonzero_without_rollback():
    """둘째가 실패해도 첫째를 되돌리지 않는다 — 실제로 이룬 상태를 그대로 알린다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, stderr = _invoke_verb(
            root, "enable", "demo", now=True, codes=(0, 1)
        )
    assert code != 0, stderr
    assert runs.verbs() == ["enable", "start"], "롤백 호출이 끼어들었다"
    assert "롤백" in stderr and "boot 허용" in stderr, stderr


def test_disable_now_partial_success_names_the_state_actually_reached():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, stderr = _invoke_verb(
            root, "disable", "demo", now=True, codes=(0, 1)
        )
    assert code != 0
    assert runs.verbs() == ["disable", "stop"], "롤백 호출이 끼어들었다"
    assert "롤백" in stderr and "boot 차단" in stderr, stderr


def test_enable_without_now_is_a_single_helper_call():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, _ = _invoke_verb(root, "enable", "demo")
    assert code == 0
    assert runs.verbs() == ["enable"], runs.argvs


def test_restart_flags_still_reach_the_single_helper_call():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, _ = _invoke_verb(root, "restart", "demo", recreate=True)
    assert code == 0
    assert runs.verbs() == ["restart"]
    assert runs.flags() == ["--recreate"], runs.argvs


def test_now_verbs_do_not_gain_a_wait_flag():
    """--wait은 단일 helper 성공 뒤의 포트 대기다 — 합성 동사에 새로 달지 않는다."""
    import runpy

    module = runpy.run_path(str(MACOSCTL), run_name="not_main")
    import argparse

    holder = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        holder["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            module["main"]()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original

    choices = {
        action.dest: action for action in holder["parser"]._subparsers._group_actions
    }["command"].choices
    for verb in ("enable", "disable"):
        flags = {
            option for action in choices[verb]._actions
            for option in action.option_strings
        }
        assert "--wait" not in flags and "--timeout" not in flags, f"{verb}: {flags}"


def test_bare_invocation_prints_help_and_succeeds():
    """다른 레포에서 온 에이전트의 첫 실행이 usage 오류로 끝나면 안 된다."""
    result = _run()
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stderr == "", result.stderr
    assert "usage:" in result.stdout
    for command in ("start", "stop", "restart", "enable", "disable", "apply"):
        assert command in result.stdout, command


def test_bare_invocation_and_help_show_the_same_command_choosing_guide():
    """단독 실행과 --help는 같은 선택 경계를 보여준다."""
    bare = _run()
    explicit = _run("--help")
    assert explicit.returncode == 0, explicit.stderr
    assert bare.stdout == explicit.stdout
    for phrase in (
        "runtime",
        "boot policy",
        "start / stop / restart",
        "enable / disable",
        "apply 불필요",
        "--now",
        "macosctl <command> --help",
    ):
        assert phrase in bare.stdout, phrase


def _verb_help(verb: str) -> str:
    result = _run(verb, "--help")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_verb_help_states_the_runtime_boot_apply_contract():
    """각 동사 --help가 축·apply 경계·예제를 스스로 말해야 한다."""
    start = _verb_help("start")
    assert "지금 기동" in start
    assert "boot 설정" in start and "그대로" in start
    assert "apply 불필요" in start
    assert "macosctl start" in start

    stop = _verb_help("stop")
    assert "지금 정지" in stop
    assert "재부팅" in stop and "macosctl disable" in stop
    assert "apply 불필요" in stop
    assert "macosctl stop" in stop

    restart = _verb_help("restart")
    assert "inactive" in restart
    assert "boot 설정" in restart and "보존" in restart
    assert "apply 불필요" in restart
    assert "--force" in restart and "--recreate" in restart and "--wait" in restart

    enable = _verb_help("enable")
    assert "boot" in enable and "허용" in enable
    assert "지금 상태는 그대로" in enable
    assert "--now" in enable and "apply 불필요" in enable

    disable = _verb_help("disable")
    assert "boot" in disable and "차단" in disable
    assert "지금 상태는 그대로" in disable
    assert "--now" in disable and "apply 불필요" in disable


def _write_second_service(root):
    fragment = root / "conf.d" / "30-demo.toml"
    (root / "conf.d" / "40-worker.toml").write_text(
        fragment.read_text().replace("demo", "worker").replace("9999", "9998")
    )


def test_control_verbs_accept_multiple_names_without_outer_sudo():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        _write_second_service(root)
        for verb in ("start", "stop", "restart", "enable", "disable"):
            code, runs, err = _invoke_verb(root, verb, ["demo", "worker"])
            assert code == 0, err
            assert [(a[3], a[4]) for a in runs.argvs] == [
                (verb, "demo"), (verb, "worker"),
            ], runs.argvs
            assert all(a[:2] == ["/usr/bin/sudo", "-n"] for a in runs.argvs)


def test_multiple_names_are_validated_before_any_helper_call():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, err = _invoke_verb(root, "stop", ["demo", "missing"])
    assert code == 2, err
    assert "그런 서비스가 없다: missing" in err, err
    assert runs.argvs == [], runs.argvs


def test_multiple_names_stop_at_first_failure_without_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        _write_second_service(root)
        code, runs, err = _invoke_verb(
            root, "stop", ["demo", "worker"], codes=(3, 0)
        )
    assert code == 3, err
    assert [a[4] for a in runs.argvs] == ["demo"], runs.argvs
    assert "demo" in err, err


def test_duplicate_names_are_executed_once():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        code, runs, err = _invoke_verb(root, "restart", ["demo", "demo"])
    assert code == 0, err
    assert runs.verbs() == ["restart"], runs.argvs


def test_multiple_names_apply_now_per_service_and_stop_on_follow_up_failure():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        _write_second_service(root)
        for verb, follow_up in (("enable", "start"), ("disable", "stop")):
            code, runs, err = _invoke_verb(root, verb, ["demo", "worker"], now=True)
            assert code == 0, err
            assert [(a[3], a[4]) for a in runs.argvs] == [
                (verb, "demo"), (follow_up, "demo"),
                (verb, "worker"), (follow_up, "worker"),
            ], runs.argvs
            code, runs, err = _invoke_verb(
                root, verb, ["demo", "worker"], now=True, codes=(0, 1)
            )
            assert code == 1, err
            assert [a[4] for a in runs.argvs] == ["demo", "demo"], runs.argvs
            assert "부분 성공" in err, err


def test_multiple_names_apply_restart_flags_and_wait_per_service():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_config(root)
        _write_second_service(root)
        for flag in ("force", "recreate"):
            waits = []
            code, runs, err = _invoke_verb(
                root, "restart", ["demo", "worker"],
                waits=waits, wait=True, timeout=7, **{flag: True},
            )
            assert code == 0, err
            assert runs.flags() == ["--" + flag, "--" + flag], runs.argvs
            assert waits == [("demo", 7), ("worker", 7)], waits


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
