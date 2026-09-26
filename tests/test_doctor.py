"""svc 관측 계층(manifest / collect / doctor) 단위 테스트."""

import io
import runpy
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import collect  # noqa: E402
from macosctl import doctor  # noqa: E402
from macosctl import inventory  # noqa: E402
from macosctl import manifest  # noqa: E402
from macosctl import model  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "drift-synthetic"


def _fixture_manifest():
    """선언과 관측은 동일한 가상 시나리오를 사용한다."""
    return manifest.load(FIXTURE / "services.toml")


def _fixture_state():
    return collect.SystemState(
        installed_labels=collect.parse_installed_labels(
            (FIXTURE / "installed-plists.txt").read_text()
        ),
        jobs=collect.parse_jobs((FIXTURE / "launchctl-print.txt").read_text()),
        listeners=collect.parse_listeners((FIXTURE / "lsof-listen.txt").read_text()),
        ppids=collect.parse_ppids((FIXTURE / "processes.txt").read_text()),
        disabled_overrides=collect.parse_disabled_overrides(
            (FIXTURE / "print-disabled.txt").read_text()
        ),
    )


def _codes_for(findings, service):
    return {f.code for f in findings if f.service == service}


def _merged(services=(), warnings=()):
    defaults = manifest.Defaults(
        user="example", working_directory="/srv", log_dir="/tmp",
        throttle_seconds=10, path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )
    return model.MergedModel(defaults, services, warnings)


def _merged_service(
    *,
    lifecycle="active",
    managed=True,
    sources=None,
    exec_argv=("/usr/bin/true",),
    env=(),
    working_directory="/srv",
    log_dir=None,
):
    return model.MergedService(
        name="demo", label="com.korellas.demo", port=9999, group="test",
        managed=managed, exec_argv=exec_argv, depends_on=(),
        mem_budget=None, env=env, working_directory=working_directory,
        log_dir=log_dir,
        lifecycle=lifecycle,
        sources=sources or (
            model.Provenance("name", "30-ai.toml"),
            model.Provenance("label", "30-ai.toml"),
            model.Provenance("port", "30-ai.toml"),
        ),
    )


def _empty_state(*, installed=(), jobs=None):
    return collect.SystemState(
        installed_labels=frozenset(installed), jobs=jobs or {}, listeners=(),
        ppids={}, disabled_overrides={},
    )


def test_doctor_reports_external_volume_tcc_risk_across_managed_services():
    direct = _merged_service(env=(("MODEL", "/Volumes/SSD/models/demo"),))
    symlinked = replace(
        _merged_service(),
        name="symlinked",
        label="com.korellas.symlinked",
        port=9998,
    )

    with tempfile.TemporaryDirectory() as tmp:
        link = Path(tmp) / "models"
        link.symlink_to("/Volumes/SSD/models", target_is_directory=True)
        symlinked = replace(symlinked, working_directory=str(link / "demo"))
        findings = doctor.diagnose_merged(
            _merged((direct, symlinked)), _empty_state(), {}
        )

    risks = [f for f in findings if f.code == "external-volume-tcc-risk"]
    assert {finding.service for finding in risks} == {"demo", "symlinked"}
    assert any("env.MODEL" in finding.detail for finding in risks)
    assert any("working_directory" in finding.detail for finding in risks)
    assert all("/Volumes/SSD/models" in finding.detail for finding in risks)
    assert all("TCC" in finding.detail for finding in risks)
    assert all("background session" in finding.detail for finding in risks)
    assert all("KeepAlive" in finding.detail for finding in risks)
    assert all("승인" in finding.action for finding in risks)
    assert all("LaunchAgent" in finding.action for finding in risks)


def test_doctor_external_volume_check_ignores_internal_and_unmanaged_paths():
    internal = _merged_service(working_directory="/Users/example/project")
    unmanaged = replace(
        _merged_service(
            managed=False,
            env=(("MODEL", "/Volumes/SSD/models/demo"),),
        ),
        name="manual",
        label="com.korellas.manual",
        port=9998,
    )

    findings = doctor.diagnose_merged(
        _merged((internal, unmanaged)), _empty_state(), {}
    )

    assert not [f for f in findings if f.code == "external-volume-tcc-risk"]


def test_doctor_reports_external_effective_log_dirs():
    overridden = _merged_service(log_dir="/Volumes/Logs/demo")
    inherited = replace(
        _merged_service(),
        name="worker",
        label="com.korellas.worker",
        port=9998,
    )
    merged = _merged((overridden, inherited))
    merged = replace(
        merged,
        defaults=replace(merged.defaults, log_dir="/Volumes/DefaultLogs"),
    )

    findings = doctor.diagnose_merged(
        merged, _empty_state(), {}
    )

    risks = [f for f in findings if f.code == "external-volume-tcc-risk"]
    assert {finding.service for finding in risks} == {"demo", "worker"}
    details = {finding.service: finding.detail for finding in risks}
    assert "log_dir → /Volumes/Logs/demo" in details["demo"]
    assert "log_dir → /Volumes/DefaultLogs" in details["worker"]


def test_orphan_dropin_is_a_standing_finding():
    merged = _merged(warnings=(
        "orphan-dropin: 대상 서비스가 없다: ghost (ghost.d)",
        "orphan-dropin: 대상 서비스가 없다: ghost (old) service "
        "(ghost (old) service.d)",
    ))
    findings = doctor.diagnose_merged(merged, _empty_state(), {})
    orphan = [finding for finding in findings if finding.code == "orphan-dropin"]
    assert {finding.service for finding in orphan} == {
        "ghost", "ghost (old) service",
    }
    assert any("ghost.d" in finding.detail for finding in orphan)
    assert doctor.exit_code(findings) == 1


def test_masked_but_installed_is_reported_without_false_positives():
    masked = _merged_service(lifecycle="masked", managed=False)
    active = _merged_service(lifecycle="active", managed=True)
    entry = inventory.Entry(None, "30-ai.toml", "masked")

    plist_state = _empty_state(installed=(masked.label,))
    codes = _codes_for(
        doctor.diagnose_merged(_merged((masked,)), plist_state, {masked.label: entry}),
        masked.name,
    )
    assert "masked-but-installed" in codes

    job_state = _empty_state(jobs={
        masked.label: collect.Job(masked.label, loaded=True, state="running", pid=42)
    })
    codes = _codes_for(
        doctor.diagnose_merged(_merged((masked,)), job_state, {masked.label: entry}),
        masked.name,
    )
    assert "masked-but-installed" in codes

    assert "masked-but-installed" not in _codes_for(
        doctor.diagnose_merged(_merged((masked,)), _empty_state(), {masked.label: entry}),
        masked.name,
    )
    assert "masked-but-installed" not in _codes_for(
        doctor.diagnose_merged(_merged((active,)), plist_state, {
            active.label: inventory.Entry("a" * 64, "30-ai.toml", "active")
        }),
        active.name,
    )

    listener_state = collect.SystemState(
        installed_labels=frozenset({masked.label}), jobs={},
        listeners=(collect.Listener(masked.port, 77, "foreign"),),
        ppids={}, disabled_overrides={},
    )
    codes = _codes_for(
        doctor.diagnose_merged(
            _merged((masked,)), listener_state, {masked.label: entry}
        ),
        masked.name,
    )
    assert "masked-but-installed" in codes
    assert "unmanaged-listener" not in codes


def test_override_summary_is_printed_even_without_drift():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_doctor"]
    merged = _merged()
    with mock.patch.dict(command.__globals__, {
        "_load_policy_model": lambda root: (merged, object()),
        "_state": lambda services, extra_labels=(): _empty_state(),
    }), mock.patch.object(inventory, "read", return_value={}):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = command(SimpleNamespace(config_root=Path("/etc/macosctl"), fixture=None))
    assert code == 0
    assert "드리프트 없음" in output.getvalue()
    assert "오버라이드 현황" in output.getvalue()


def test_override_summary_names_the_winning_file():
    service = _merged_service(sources=(
        model.Provenance("name", "30-ai.toml"),
        model.Provenance("port", "30-ai.toml"),
        model.Provenance("port", "70-local.toml"),
    ))
    summary = doctor.override_summary(_merged((service,)))
    assert len(summary) == 1
    assert summary[0].service == "demo"
    assert summary[0].field == "port"
    assert summary[0].winner == "70-local.toml"


def test_doctor_inventory_read_error_warns_without_losing_other_diagnosis():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_doctor"]
    service = _merged_service()
    merged = _merged((service,))
    with mock.patch.dict(command.__globals__, {
        "_load_policy_model": lambda root: (merged, object()),
        "_state": lambda services, extra_labels=(): _empty_state(),
    }), mock.patch.object(
        inventory, "read", side_effect=inventory.InventoryCorrupt("broken inventory")
    ):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = command(SimpleNamespace(config_root=Path("/etc/macosctl"), fixture=None))
    assert code == 1
    assert "broken inventory" in output.getvalue()
    assert "plist-missing" in output.getvalue()
    assert "inventory-corrupt" in output.getvalue()
    assert "드리프트 없음" not in output.getvalue()
    assert "오버라이드 현황" in output.getvalue()
    assert "Traceback" not in output.getvalue()


def test_cli_doctor_collects_inventory_only_masked_job_and_reports_it():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_doctor"]
    merged = _merged()
    label = "com.korellas.unlinked"
    entry = inventory.Entry(None, "30-old.toml", "masked")
    collected = []

    def collect_live(*, labels, ports):
        collected.extend(labels)
        return _empty_state(jobs={
            label: collect.Job(label, loaded=True, state="running", pid=42)
        })

    with mock.patch.dict(command.__globals__, {
        "_load_policy_model": lambda root: (merged, object()),
    }), mock.patch.object(inventory, "read", return_value={label: entry}), \
         mock.patch.object(namespace["collect"], "collect_live", side_effect=collect_live):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = command(SimpleNamespace(config_root=Path("/etc/macosctl"), fixture=None))
    assert code == 1
    assert collected == [label]
    assert "masked-but-installed" in output.getvalue()
    assert label in output.getvalue()


def test_state_collection_deduplicates_merged_and_extra_labels():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    state_fn = namespace["_state"]
    service = _merged_service(lifecycle="masked", managed=False)
    seen = []

    def collect_live(*, labels, ports):
        seen.extend(labels)
        return _empty_state()

    with mock.patch.object(namespace["collect"], "collect_live", side_effect=collect_live):
        state_fn((service,), extra_labels=(service.label, service.label))
    assert seen == [service.label]


def test_doctor_detects_all_2026_08_15_drift():
    """설계 스펙 §7-1 — 이 스펙의 근본 요구사항 테스트."""
    services = _fixture_manifest()
    findings = doctor.diagnose(services, _fixture_state())

    # ① 매니페스트(managed)에 있는데 plist 없음
    assert "plist-missing" in _codes_for(findings, "worker-missing")
    assert "plist-missing" in _codes_for(findings, "worker-a")
    assert "plist-missing" in _codes_for(findings, "worker-b")

    # ③ managed인데 다운 (포트에 아무도 없음).
    #
    # 등록이 아예 없는 상태의 단일 조치는 apply 하나뿐이다 — 예전처럼
    # plist-missing과 down을 함께 올리면 조치가 둘이 되어 아무것도 좁히지 못한다.
    codes = _codes_for(findings, "worker-missing")
    assert codes == {"plist-missing"}, codes

    # ④ 포트 LISTEN인데 launchd job 트리 밖 (tmux 우회 기동)
    assert "rogue-listener" in _codes_for(findings, "worker-a")
    assert "rogue-listener" in _codes_for(findings, "worker-b")

    # ⑤ launchd 인스턴스와 별개 프로세스 공존 (gateway 이중 기동)
    assert "duplicate-instance" in _codes_for(findings, "gateway")


def test_doctor_does_not_report_enabled_override_residue():
    """D4 재정의 — enabled 잔재는 지울 수 없는 무해한 찌꺼기다. 보고하면 오탐."""
    services = _fixture_manifest()
    findings = doctor.diagnose(services, _fixture_state())
    stale = [f for f in findings if f.code == "stale-disabled-override"]
    assert stale == [], f"enabled 잔재를 드리프트로 보고했다: {stale}"


def test_doctor_ignores_labels_outside_our_namespace():
    """Apple 데몬의 disabled 상태를 우리 드리프트로 보고하면 안 된다.

    """
    services = _fixture_manifest()
    state = collect.SystemState(
        installed_labels=frozenset(),
        jobs={},
        listeners=(),
        ppids={},
        disabled_overrides={
            "com.apple.ftpd": True,
            "com.apple.bootpd": True,
            "com.korellas.retired-a": True,  # 우리 것 — 이건 보고해야 한다
        },
    )
    stale = {f.service for f in doctor.diagnose(services, state)
             if f.code == "stale-disabled-override"}
    assert stale == {"com.korellas.retired-a"}, stale


def test_doctor_does_not_flag_unmanaged_service_as_down():
    """managed=false는 '다운이 정상'이다 (manual-worker, worker-disabled)."""
    services = _fixture_manifest()
    findings = doctor.diagnose(services, _fixture_state())
    assert "down" not in _codes_for(findings, "manual-worker")
    assert "down" not in _codes_for(findings, "worker-disabled")


def test_doctor_treats_child_listener_as_owned():
    """부모 래퍼가 job이고 자식이 포트를 쥐는 구조(run-mtplx.sh)는 정상이다.

    PID 동일성 대신 프로세스 트리 소속으로 판정해야 한다.
    """
    services = _fixture_manifest()
    state = collect.SystemState(
        installed_labels=frozenset({"com.korellas.worker-missing"}),
        jobs={
            "com.korellas.worker-missing": collect.Job(
                label="com.korellas.worker-missing",
                loaded=True,
                state="running",
                pid=301,
            )
        },
        listeners=(collect.Listener(port=18000, pid=302, command="python3.13"),),
        ppids={302: 301, 301: 1},
        disabled_overrides={},
    )
    findings = doctor.diagnose(services, state)
    codes = _codes_for(findings, "worker-missing")
    assert "rogue-listener" not in codes
    assert "down" not in codes


def test_doctor_reports_clean_system_as_no_findings():
    """순수 진단은 정상 정보 요약과 별개로 드리프트 Finding을 만들지 않는다."""
    services = tuple(s for s in _fixture_manifest() if s.name == "gateway")
    state = collect.SystemState(
        installed_labels=frozenset({"com.korellas.gateway"}),
        jobs={
            "com.korellas.gateway": collect.Job(
                label="com.korellas.gateway", loaded=True, state="running", pid=303
            )
        },
        listeners=(collect.Listener(port=14000, pid=303, command="python3.1"),),
        ppids={303: 1},
        disabled_overrides={"com.korellas.gateway": False},
    )
    assert doctor.diagnose(services, state) == ()


def test_doctor_exit_code_reflects_findings():
    assert doctor.exit_code(()) == 0
    assert doctor.exit_code((doctor.Finding("down", "x", "d", "a"),)) == 1


def test_cli_doctor_reads_policy_and_reports_security_refusal_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "conf.d").mkdir()
        (root / "macosctl.toml").write_text(
            'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
        )
        (root / "policy.json").write_text(
            '{"schema":1,"label_prefix":"com.korellas.",'
            '"label_exceptions":{},"service_user":"example",'
            '"groups":["infra"]}'
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "doctor",
                "--fixture",
                str(FIXTURE),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "설정 디렉터리" in output
    assert "Traceback" not in output


def test_doctor_reports_disabled_override_on_declared_service():
    """선언·managed인데 disabled 오버라이드가 남아 있으면 보고해야 한다.

    기존 stale-disabled-override는 `label not in declared_labels` 조건이라,
    서비스가 다시 선언되는 순간 침묵한다. 그런데 바로 그 상태가 위험하다 —
    apply의 bootstrap이 119로 실패하는데 doctor는 아무 말도 하지 않는다.
    """
    services = _fixture_manifest()
    declared = next(s for s in services if s.managed)
    state = collect.SystemState(
        installed_labels=frozenset({declared.label}),
        jobs={},
        listeners=(),
        ppids={},
        disabled_overrides={declared.label: True},
    )
    codes = {f.code for f in doctor.diagnose(services, state)
             if f.service == declared.label}
    assert "declared-but-disabled" in codes, (
        f"선언된 서비스의 disabled 오버라이드를 보고하지 않았다: {codes}"
    )


def test_doctor_disabled_override_action_does_not_tell_user_to_call_launchctl():
    """조치 문구가 CLAUDE.md 금지 조항과 충돌하면 안 된다.

    `launchctl enable`은 이 스택에서 금지된 직접 호출이고, 정도(正道)는
    `svc enable <name>`이다. 해소 불가능한 안내는 안내가 아니다.
    """
    services = _fixture_manifest()
    state = collect.SystemState(
        installed_labels=frozenset(),
        jobs={},
        listeners=(),
        ppids={},
        disabled_overrides={"com.korellas.retired-a": True},
    )
    for finding in doctor.diagnose(services, state):
        if "disabled" in finding.code:
            assert "launchctl" not in finding.action, (
                f"{finding.code}의 조치가 금지된 직접 호출을 안내한다: {finding.action}"
            )


def test_doctor_stays_silent_on_deliberately_disabled_service():
    """운영자가 `svc disable`한 서비스를 드리프트로 보고하면 안 된다.

    2R에서 Fable이 잡았다 — declared-but-disabled가 state 마크를 안 봐서, D6이
    보장한 "동사들이 진단과 싸우지 않는다"가 doctor 쪽에서 깨졌다. 훅·CI가
    exit code를 소비하므로 거짓 경보가 상시화된다.
    """
    services = _fixture_manifest()
    svc = next(s for s in services if s.managed)
    state = collect.SystemState(
        installed_labels=frozenset({svc.label}),
        jobs={}, listeners=(), ppids={},
        disabled_overrides={svc.label: True},
        disabled_by_macosctl=frozenset({svc.label}),  # macosctl이 그렇게 만들었다
    )
    codes = {f.code for f in doctor.diagnose(services, state) if f.service in (svc.label, svc.name)}
    assert "declared-but-disabled" not in codes, (
        f"의도적 disable을 드리프트로 보고했다: {codes}"
    )


def test_doctor_still_reports_disabled_without_operator_mark():
    """마크 없이 오버라이드만 disabled인 것이 진짜 불일치다 — 그건 계속 보고한다."""
    services = _fixture_manifest()
    svc = next(s for s in services if s.managed)
    state = collect.SystemState(
        installed_labels=frozenset({svc.label}),
        jobs={}, listeners=(), ppids={},
        disabled_overrides={svc.label: True},
        disabled_by_macosctl=frozenset(),    # 아무도 그렇게 만든 적 없다
    )
    codes = {f.code for f in doctor.diagnose(services, state)}
    assert "declared-but-disabled" in codes, codes


def test_stale_disabled_override_action_is_actually_achievable():
    """조치가 실행 가능해야 한다.

    이미 은퇴해 인벤토리 밖인 라벨은 `svc apply`로 해소되지 않는다 — 은퇴할 대상
    자체가 계획에 없기 때문이다. 되지 않는 일을 안내하면 안 된다.
    """
    services = _fixture_manifest()
    state = collect.SystemState(
        installed_labels=frozenset(), jobs={}, listeners=(), ppids={},
        disabled_overrides={"com.korellas.retired-a": True},
    )
    stale = [f for f in doctor.diagnose(services, state)
             if f.code == "stale-disabled-override"]
    assert stale, "stale 오버라이드를 보고하지 않았다"
    action = stale[0].action
    assert "은퇴" not in action, f"이미 은퇴한 라벨에 '은퇴하라'고 안내한다: {action}"
    assert "선언" in action and "apply" in action, (
        f"실행 가능한 복구 경로를 안내하지 않는다: {action}"
    )


# --- 정확한 단일 조치 -------------------------------------------------------------


def _one(findings, code):
    matches = [f for f in findings if f.code == code]
    assert len(matches) == 1, f"{code}: {[f.code for f in findings]}"
    return matches[0]


def _job(label, *, loaded=True, pid=None, state_name="running"):
    return {label: collect.Job(label=label, loaded=loaded, pid=pid,
                               state=state_name if loaded else None)}


def _state(*, installed=(), jobs=None, listeners=(), ppids=None,
           overrides=None, stopped=(), provenance=()):
    return collect.SystemState(
        installed_labels=frozenset(installed), jobs=jobs or {},
        listeners=tuple(listeners), ppids=ppids or {},
        disabled_overrides=overrides or {},
        stopped_this_boot=frozenset(stopped),
        disabled_by_macosctl=frozenset(provenance),
    )


def _demo():
    return (_merged_service(),)


LABEL = "com.korellas.demo"


def test_missing_plist_action_is_a_privileged_apply():
    findings = doctor.diagnose(_demo(), _state())
    assert _one(findings, "plist-missing").action == "sudo macosctl apply"


def test_installed_but_no_job_action_is_start():
    findings = doctor.diagnose(_demo(), _state(installed=[LABEL]))
    assert _one(findings, "not-running").action == "macosctl start demo"


def test_loaded_without_pid_action_is_a_recreating_restart():
    findings = doctor.diagnose(
        _demo(), _state(installed=[LABEL], jobs=_job(LABEL, pid=None))
    )
    finding = _one(findings, "loaded-without-pid")
    assert finding.action == "macosctl restart demo --recreate"


def test_pid_without_an_owned_listener_action_is_logs_then_waiting_restart():
    findings = doctor.diagnose(
        _demo(), _state(installed=[LABEL], jobs=_job(LABEL, pid=4242))
    )
    finding = _one(findings, "no-owned-listener")
    assert finding.action == "macosctl logs demo 확인 후 macosctl restart demo --wait"


def test_stop_intent_for_this_boot_action_is_start():
    findings = doctor.diagnose(
        _demo(), _state(installed=[LABEL], stopped=[LABEL])
    )
    assert _one(findings, "stopped-by-operator").action == "macosctl start demo"


def test_stop_intent_from_a_previous_boot_is_already_expired_and_silent():
    """boot session 의미론이 만료를 처리한다 — doctor가 따로 지울 것이 없다."""
    findings = doctor.diagnose(
        _demo(), _state(installed=[LABEL], jobs=_job(LABEL, pid=1), stopped=())
    )
    assert [f.code for f in findings if f.code == "stale-stop-mark"] == []


def test_stale_stop_intent_is_reported_once_with_a_single_action():
    listener = collect.Listener(port=9999, pid=1, command="demo")
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=1), listeners=[listener],
               ppids={1: 1}, stopped=[LABEL]),
    )
    assert _one(findings, "stale-stop-mark").action == "macosctl start demo"


def test_actual_disable_with_macosctl_provenance_is_intentional_and_silent():
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=1),
               listeners=[collect.Listener(port=9999, pid=1, command="demo")],
               ppids={1: 1}, overrides={LABEL: True}, provenance=[LABEL]),
    )
    assert findings == (), findings


def test_actual_disable_without_provenance_action_is_enable():
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=1),
               listeners=[collect.Listener(port=9999, pid=1, command="demo")],
               ppids={1: 1}, overrides={LABEL: True}),
    )
    assert _one(findings, "declared-but-disabled").action == "macosctl enable demo"


def test_stale_disable_provenance_on_an_enabled_service_has_one_action():
    """state는 'macosctl이 disable했다'는데 launchd는 enabled — 출처가 낡았다."""
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=1),
               listeners=[collect.Listener(port=9999, pid=1, command="demo")],
               ppids={1: 1}, overrides={LABEL: False}, provenance=[LABEL]),
    )
    assert _one(findings, "stale-disable-provenance").action == "macosctl enable demo"


def test_no_finding_ever_offers_two_alternatives():
    """'apply / start' 같은 복수 대안은 조치가 아니다."""
    scenarios = (
        _state(),
        _state(installed=[LABEL]),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=None)),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=7)),
        _state(installed=[LABEL], stopped=[LABEL]),
        _state(installed=[LABEL], overrides={LABEL: True}),
        _state(installed=[LABEL], jobs=_job(LABEL, loaded=False),
               listeners=[collect.Listener(port=9999, pid=31, command="rogue")],
               ppids={31: 1}),
    )
    for state in scenarios:
        for finding in doctor.diagnose(_demo(), state):
            assert " / " not in finding.action, (
                f"{finding.code}의 조치가 복수 대안이다: {finding.action}"
            )



# --- inactive + boot-disabled는 장애가 아니라 유효한 상태다 (C4) ----------------------
#
# enable/disable은 boot policy만 바꾸고 runtime을 보존한다. 그래서 "설치돼 있고,
# 부팅 기동이 막혀 있고, 지금 안 떠 있다"는 조합은 정상적으로 도달 가능한 상태다.
# 그것을 runtime 장애로 올리면 doctor가 `start`를 권하고, start는 boot policy를
# 그대로 둔 채 기동하므로 다음 진단에서 같은 경고가 다시 뜬다 — 해소되지 않는다.


def test_intentional_inactive_and_boot_disabled_is_not_a_runtime_fault():
    """실제 disabled + provenance + job 없음 = 의도한 상태다. 조용해야 한다."""
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], overrides={LABEL: True}, provenance=[LABEL]),
    )
    assert findings == (), findings


def test_boot_disabled_without_provenance_offers_only_the_enable_action():
    """아무도 의도하지 않은 disabled의 조치는 enable 하나뿐이다.

    같은 실행에서 not-running/start를 함께 권하면 조치가 둘이 되고, 그중 start는
    boot policy를 건드리지 않아 원인을 해소하지 못한다.
    """
    findings = doctor.diagnose(
        _demo(), _state(installed=[LABEL], overrides={LABEL: True})
    )
    assert [f.code for f in findings] == ["declared-but-disabled"], findings
    assert _one(findings, "declared-but-disabled").action == "macosctl enable demo"


def test_stop_intent_for_this_boot_wins_over_boot_disabled():
    """이번 부팅의 정지 의도가 더 구체적이다 — 그쪽 조치를 유지한다."""
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], overrides={LABEL: True}, provenance=[LABEL],
               stopped=[LABEL]),
    )
    assert [f.code for f in findings] == ["stopped-by-operator"], findings
    assert _one(findings, "stopped-by-operator").action == "macosctl start demo"


def test_loaded_without_pid_is_still_reported_when_boot_disabled():
    """disabled라는 이유로 등록 계층의 고장까지 숨기면 안 된다."""
    findings = doctor.diagnose(
        _demo(),
        _state(installed=[LABEL], jobs=_job(LABEL, pid=None),
               overrides={LABEL: True}, provenance=[LABEL]),
    )
    assert [f.code for f in findings] == ["loaded-without-pid"], findings
    assert _one(findings, "loaded-without-pid").action == (
        "macosctl restart demo --recreate"
    )


def test_boot_enabled_inactive_service_still_reports_not_running():
    """대조군 — 부팅 기동이 열려 있는데 안 떠 있으면 그건 여전히 장애다."""
    findings = doctor.diagnose(_demo(), _state(installed=[LABEL]))
    assert _one(findings, "not-running").action == "macosctl start demo"


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
