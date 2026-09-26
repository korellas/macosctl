"""svc reconcile 계층 apply 단위 테스트."""

import json
import os
import plistlib
import runpy
import sys
import tempfile
import subprocess
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
MOVED_REPO = Path("/private/tmp/macosctl-moved-repo")
sys.path.insert(0, str(REPO))

from macosctl import inventory  # noqa: E402
from macosctl import manifest  # noqa: E402
from macosctl import model  # noqa: E402
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


def _fake_launchctl(calls, rc_for=None):
    """launchctl 호출을 가로채 순서를 기록한다. rc_for는 {동사: 반환코드}."""
    import subprocess as sp

    # print는 기본 113(그런 서비스 없음) — 실측한 launchctl의 반환코드다.
    # 임의의 non-zero를 쓰면 P0.3 이후 "알 수 없는 오류"로 분류돼 30초를 폴링한다.
    codes = {"print": 113}
    codes.update(rc_for or {})

    def fake(*args):
        calls.append(args)
        verb = args[0] if args else ""
        return sp.CompletedProcess(args, codes.get(verb, 0), stdout="", stderr="")

    return fake


def _demo_service():
    return _service(name="demo", label="com.korellas.demo")


def _merged(services, defaults):
    return model.MergedModel(
        defaults,
        tuple(model.MergedService(
            name=service.name, label=service.label, port=service.port,
            group=service.group, managed=service.managed,
            exec_argv=service.exec_argv, depends_on=service.depends_on,
            mem_budget=service.mem_budget, env=service.env,
            working_directory=defaults.working_directory, lifecycle="active",
            sources=(model.Provenance("name", "services.toml"),),
        ) for service in services),
        (),
    )


def _entries(labels):
    return {
        label: inventory.Entry("a" * 64, "services.toml", "active")
        for label in labels
    }


def _write_staging_config(root: Path) -> None:
    root.chmod(0o755)
    (root / "conf.d").mkdir()
    (root / "macosctl.toml").write_text(
        'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
    )
    (root / "policy.json").write_text(json.dumps({
        "schema": 1,
        "label_prefix": "com.korellas.",
        "label_exceptions": {"webtop": "com.webtop"},
        "service_user": "example",
        "groups": ["infra", "dashboard"],
    }))
    (root / "policy.json").chmod(0o644)


def test_authorized_service_user_survives_bind_to_desired_plist():
    from macosctl import apply, policy

    defaults = manifest.load_defaults(MANIFEST)
    unbound = model.MergedModel(
        manifest.Defaults(**{**defaults.__dict__, "user": None}),
        (
            model.MergedService(
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
                sources=(model.Provenance("name", "30-render.toml"),),
                user="_render",
            ),
        ),
        (),
    )
    bound = policy.bind(
        unbound,
        policy.Policy(
            label_prefix="com.korellas.",
            label_exceptions={},
            service_user="example",
            groups=("test",),
            service_users={"render": "_render"},
        ),
    )

    units = apply.desired_units(
        bound.services,
        bound.defaults,
        REPO,
        Path("/etc/macosctl"),
    )
    service_unit = next(unit for unit in units if unit.name == "render")

    assert plistlib.loads(service_unit.body)["UserName"] == "_render"


def test_cli_apply_dry_run_accepts_secure_caller_owned_staging_policy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_staging_config(root)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "apply",
                "--dry-run",
                "--inventory",
                str(root / "inventory"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "policy 경계 미연결" not in output
    assert "Traceback" not in output
    assert "--dry-run" in output


def test_cli_apply_dry_run_scopes_to_named_service():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_staging_config(root)
        (root / "conf.d" / "30-demo.toml").write_text(
            'schema = 1\n\n[[service]]\nname = "demo"\n'
            'label = "com.korellas.demo"\nport = 9999\ngroup = "infra"\n'
            'exec = ["/bin/echo", "demo"]\ndepends_on = []\n'
            'working_directory = "/tmp"\n'
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "apply",
                "--dry-run",
                "--service",
                "demo",
                "--inventory",
                str(root / "inventory"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "demo" in output
    assert "log-rotate" not in output


def test_select_services_removes_every_unselected_action():
    from macosctl import apply

    target = apply.Unit("com.korellas.target", "target", b"target", "target.toml")
    other = apply.Unit("com.korellas.other", "other", b"other", "other.toml")
    plan = apply.Plan(
        create=(other,),
        changed=(target,),
        unchanged=(other,),
        retire=("com.korellas.other",),
        mask=("com.korellas.other",),
        mask_sources=(("com.korellas.other", "other.toml"),),
        inventory_missing=True,
    )
    services = (
        _service(name="target", label="com.korellas.target"),
        _service(name="other", label="com.korellas.other"),
    )

    selected = apply.select_services(plan, services, ("target",))

    assert selected.create == ()
    assert selected.changed == (target,)
    assert selected.unchanged == ()
    assert selected.retire == ()
    assert selected.mask == ()
    assert selected.mask_sources == ()
    assert selected.inventory_missing is True


def test_cli_apply_mutating_custom_root_still_requires_strict_root_policy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_staging_config(root)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "apply",
                "--inventory",
                str(root / "inventory"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "설정 디렉터리" in output
    assert "root가 필요하다" not in output
    assert "Traceback" not in output


def test_cli_apply_reports_invalid_utf8_inventory_without_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_staging_config(root)
        inv_path = root / "inventory"
        inv_path.write_bytes(b"\xff")
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "apply",
                "--dry-run",
                "--inventory",
                str(inv_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "인벤토리가 손상됐다" in output
    assert "Traceback" not in output


def test_apply_policy_reader_never_uses_staging_for_default_or_mutating_paths():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    choose = namespace["_read_policy_for_apply"]
    cli_policy = namespace["policy"]
    calls = []
    original_read = cli_policy.read
    original_staging = cli_policy.read_staging
    cli_policy.read = lambda path: calls.append(("strict", path)) or "strict"
    cli_policy.read_staging = (
        lambda path: calls.append(("staging", path)) or "staging"
    )
    try:
        assert choose(SimpleNamespace(
            dry_run=True, config_root=namespace["confd"].CONFIG_ROOT
        )) == "strict"
        assert choose(SimpleNamespace(
            dry_run=False, config_root=Path("/tmp/custom-svc")
        )) == "strict"
        assert choose(SimpleNamespace(
            dry_run=True, config_root=Path("/tmp/custom-svc")
        )) == "staging"
    finally:
        cli_policy.read = original_read
        cli_policy.read_staging = original_staging

    assert [kind for kind, _ in calls] == ["strict", "strict", "staging"]


def test_plan_passes_selected_config_root_to_log_rotate():
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    config_root = Path("/private/tmp/custom-macosctl")
    with tempfile.TemporaryDirectory() as tmp:
        plan = apply.build_plan(
            _merged((), defaults),
            defaults,
            REPO,
            known_inventory={},
            daemon_dir=Path(tmp),
            config_root=config_root,
        )

    unit = next(u for u in plan.create if u.label == apply.LOG_ROTATE_LABEL)
    payload = plistlib.loads(unit.body)
    assert payload["EnvironmentVariables"]["MACOSCTL_CONFIG"] == str(
        config_root / "macosctl.toml"
    )


def test_plan_changes_only_log_rotate_after_the_move():
    """무변경 apply는 서비스 0개 재기동 — Phase 2의 완료 기준.

    독립 레포로 옮긴 뒤 log-rotate plist는 반드시 '변경'이다 —
    ProgramArguments와 WorkingDirectory에 레포 경로가 박혀 있다 (스펙 D11-1).
    서비스가 하나라도 끼면 이식이 틀린 것이다.
    """
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    services = manifest.managed_only(manifest.load(MANIFEST))
    with tempfile.TemporaryDirectory() as tmp:
        daemon_dir = Path(tmp)
        for service in services:
            (daemon_dir / f"{service.label}.plist").write_bytes(
                plistgen.build(service, defaults)
            )
        (daemon_dir / f"{apply.LOG_ROTATE_LABEL}.plist").write_bytes(
            plistgen.build_log_rotate(defaults, REPO, Path("/etc/macosctl/macosctl.toml"))
        )
        plan = apply.build_plan(_merged(services, defaults), defaults, MOVED_REPO,
                                known_inventory=_entries(s.label for s in services),
                                daemon_dir=daemon_dir,
                                config_root=Path("/etc/macosctl"))
    changed = {u.label for u in plan.changed}
    assert changed == {"com.korellas.log-rotate"}, f"예상 밖 변경: {changed}"


def test_plan_retires_only_inventory_members():
    """인벤토리 밖 plist는 어떤 접두사든 건드리지 않는다 (2R 양쪽 공통 CRITICAL)."""
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    services = manifest.managed_only(manifest.load(MANIFEST))
    desired = {s.label for s in services} | {"com.korellas.log-rotate"}
    plan = apply.build_plan(
        _merged(services, defaults), defaults, REPO,
        known_inventory=_entries(desired | {"com.korellas.retired-thing"}),
        daemon_dir=DAEMON_DIR,
        config_root=Path("/etc/macosctl"),
    )
    assert plan.retire == ("com.korellas.retired-thing",), plan.retire


def test_plan_never_retires_log_rotate():
    """log-rotate는 매니페스트 밖이지만 우리가 설치하는 정당한 잡이다."""
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    services = manifest.managed_only(manifest.load(MANIFEST))
    plan = apply.build_plan(
        _merged(services, defaults), defaults, REPO,
        known_inventory=_entries(
            {s.label for s in services} | {"com.korellas.log-rotate"}
        ),
        daemon_dir=DAEMON_DIR,
        config_root=Path("/etc/macosctl"),
    )
    assert "com.korellas.log-rotate" not in plan.retire


def test_plan_with_missing_inventory_refuses_to_retire():
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    services = manifest.managed_only(manifest.load(MANIFEST))
    plan = apply.build_plan(_merged(services, defaults), defaults, REPO,
                            known_inventory=None, daemon_dir=DAEMON_DIR,
                            config_root=Path("/etc/macosctl"))
    assert plan.retire == ()
    assert plan.inventory_missing is True


def test_plan_treats_zero_managed_services_as_valid():
    """전부 내리는 것은 정상 상태고 log-rotate만 변경이다."""
    from macosctl import apply

    defaults = manifest.load_defaults(MANIFEST)
    plan = apply.build_plan(_merged((), defaults), defaults, MOVED_REPO,
                            known_inventory=_entries({"com.korellas.gone"}),
                            daemon_dir=DAEMON_DIR,
                            config_root=Path("/etc/macosctl"))
    assert plan.retire == ("com.korellas.gone",)
    assert plan.create == ()
    changed = {u.label for u in plan.changed}
    assert changed == {"com.korellas.log-rotate"}, f"예상 밖 변경: {changed}"


def test_enable_precedes_bootstrap_when_installing():
    """disabled 라벨은 bootstrap이 119로 죽는다 — enable이 먼저여야 한다.

    `launchctl bootstrap`은 disabled 오버라이드가 걸린 서비스에 대해
    'Bootstrap failed: 119: Service is disabled'로 실패한다. enable은 로드 여부와
    무관하게 영속 DB만 토글하므로 앞에 두는 것이 안전하다.
    """
    from macosctl import apply
    from macosctl import plist as plistgen

    calls = []
    original = apply._launchctl
    apply._launchctl = _fake_launchctl(calls)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"
            daemon_dir.mkdir()
            svc = _demo_service()
            body = plistgen.build(svc, manifest.load_defaults(MANIFEST))
            plan = apply.Plan(create=(apply.Unit(svc.label, svc.name, body),))
            apply.execute(plan, inventory_path=tmpdir / "inv",
                          daemon_dir=daemon_dir, lock_path=tmpdir / "lock", state_path=tmpdir / "state",
                          apply_lock_path=tmpdir / "apply.lock",
                          log=lambda *_: None)

    finally:
        apply._launchctl = original

    verbs = [c[0] for c in calls]
    assert "enable" in verbs and "bootstrap" in verbs, verbs
    assert verbs.index("enable") < verbs.index("bootstrap"), (
        f"bootstrap이 enable보다 먼저다 — disabled 서비스는 119로 죽는다: {verbs}"
    )


def test_retire_clears_disabled_override():
    """은퇴하는 라벨의 disabled 오버라이드를 enabled로 되돌려야 한다.

    launchctl에는 오버라이드 항목을 삭제하는 verb가 없다. disabled인 채로 은퇴하면
    같은 라벨을 재등록했을 때 bootstrap이 119로 영구 실패한다.
    """
    from macosctl import apply

    calls = []
    original = apply._launchctl
    apply._launchctl = _fake_launchctl(calls)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"
            daemon_dir.mkdir()
            (daemon_dir / "com.korellas.gone.plist").write_bytes(b"<plist/>")
            inv_path = tmpdir / "inv"
            inventory.write(inv_path, {
                "com.korellas.gone": inventory.Entry(
                    "c" * 64, "old.toml", "active"
                )
            })
            plan = apply.Plan(retire=("com.korellas.gone",))
            apply.execute(plan, inventory_path=inv_path,
                          daemon_dir=daemon_dir, lock_path=tmpdir / "lock", state_path=tmpdir / "state",
                          apply_lock_path=tmpdir / "apply.lock",
                          log=lambda *_: None)
    finally:
        apply._launchctl = original

    assert ("enable", "system/com.korellas.gone") in calls, (
        f"은퇴하면서 disabled 오버라이드를 청소하지 않았다: {calls}"
    )


def test_retire_aborts_when_bootout_does_not_finish():
    """bootout이 안 끝나면 은퇴를 중단한다 — 제어 수단을 먼저 잃으면 안 된다.

    지금은 _bootout_and_wait의 반환값을 무시하고 plist·인벤토리·state를 지운다.
    그러면 프로세스는 계속 도는데 인벤토리에서 빠져 svcctl이 stop/restart를
    거부한다 — 실행 중이면서 제어 불가능한 상태다.
    """
    from macosctl import apply
    from macosctl import state

    original_boot = apply._bootout_and_wait
    original_lc = apply._launchctl
    apply._bootout_and_wait = lambda label: False   # 30초 타임아웃 재현
    apply._launchctl = _fake_launchctl([])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"
            daemon_dir.mkdir()
            plist = daemon_dir / "com.korellas.stuck.plist"
            plist.write_bytes(b"<plist/>")
            inv_path, state_path = tmpdir / "inv", tmpdir / "state"
            inventory.write(inv_path, {
                "com.korellas.stuck": inventory.Entry(
                    "d" * 64, "old.toml", "active"
                )
            })
            boot_uuid = state.boot_session_uuid()
            state.record_stopped(state_path, "com.korellas.stuck", boot_uuid)

            plan = apply.Plan(retire=("com.korellas.stuck",))
            result = apply.execute(plan, inventory_path=inv_path,
                                   daemon_dir=daemon_dir, lock_path=tmpdir / "lock", state_path=state_path,
                                   apply_lock_path=tmpdir / "apply.lock",
                                   log=lambda *_: None)

            assert plist.exists(), "bootout 실패인데 plist를 지웠다"
            assert "com.korellas.stuck" in (inventory.read(inv_path) or {}), (
                "bootout 실패인데 인벤토리에서 뺐다 — svcctl 제어가 막힌다"
            )
            assert state.read(state_path).is_stopped_this_boot(
                "com.korellas.stuck", boot_uuid
            ), "bootout 실패인데 정지 의도를 지웠다"
            assert result.retired == [], "은퇴하지 못했는데 성공으로 셌다"
            assert result.failed, "은퇴 실패를 보고하지 않았다"
    finally:
        apply._bootout_and_wait = original_boot
        apply._launchctl = original_lc


def test_execute_keeps_inventory_entry_when_plist_write_fails():
    """변경분 쓰기가 실패해도 구 항목이 인벤토리에 남아야 한다.

    지금은 members를 plan.unchanged로만 시작하므로, 변경 대상의 쓰기가 실패하면
    구 plist는 설치된 채인데 인벤토리에서 빠진다 — svcctl 인가가 막히고, 이후
    안전한 은퇴도 불가능한 고아가 된다.
    """
    from macosctl import apply

    original_write = apply._write_plist_atomic
    original_lc = apply._launchctl

    def boom(target, body):
        raise OSError("ENOSPC")

    apply._write_plist_atomic = boom
    apply._launchctl = _fake_launchctl([])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"
            daemon_dir.mkdir()
            (daemon_dir / "com.korellas.demo.plist").write_bytes(b"<old/>")
            inv_path = tmpdir / "inv"
            old_hash = "0" * 64
            old_entry = inventory.Entry(old_hash, "30-ai.toml", "active")
            inventory.write(inv_path, {"com.korellas.demo": old_entry})

            plan = apply.Plan(
                changed=(apply.Unit("com.korellas.demo", "demo", b"<new/>"),)
            )
            result = apply.execute(plan, inventory_path=inv_path,
                                   daemon_dir=daemon_dir, lock_path=tmpdir / "lock", state_path=tmpdir / "state",
                                   known_inventory={"com.korellas.demo": old_entry},
                                   apply_lock_path=tmpdir / "apply.lock",
                                   log=lambda *_: None)

            assert result.failed, "쓰기 실패를 보고하지 않았다"
            members = inventory.read(inv_path) or {}
            assert members["com.korellas.demo"].sha256 == old_hash, (
                f"쓰기 실패로 인벤토리 고아가 생겼다: {members}"
            )
    finally:
        apply._write_plist_atomic = original_write
        apply._launchctl = original_lc


def test_retire_aborts_when_enable_restore_fails():
    """disabled 복원이 실패하면 은퇴를 강행하지 않는다.

    2R Codex — F2가 enable 반환값을 안 봤다. 실패한 채 plist·인벤토리를 지우면
    disabled 오버라이드가 영구히 남아, 같은 label 재등록이 119로 막힌다.
    """
    from macosctl import apply

    calls = []
    original = apply._launchctl
    apply._launchctl = _fake_launchctl(calls, rc_for={"enable": 1})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"; daemon_dir.mkdir()
            plist = daemon_dir / "com.korellas.gone.plist"
            plist.write_bytes(b"<plist/>")
            inv_path = tmpdir / "inv"
            inventory.write(inv_path, {
                "com.korellas.gone": inventory.Entry(
                    "c" * 64, "old.toml", "active"
                )
            })

            result = apply.execute(
                apply.Plan(retire=("com.korellas.gone",)),
                inventory_path=inv_path, daemon_dir=daemon_dir,
                state_path=tmpdir / "state", apply_lock_path=tmpdir / "apply.lock",
                log=lambda *_: None,
            )
            assert plist.exists(), "enable 복원 실패인데 plist를 지웠다"
            assert "com.korellas.gone" in (inventory.read(inv_path) or {}), \
                "enable 복원 실패인데 인벤토리에서 뺐다"
            assert result.failed, "enable 복원 실패를 보고하지 않았다"
    finally:
        apply._launchctl = original


def test_job_exists_distinguishes_absent_from_error():
    """`launchctl print`의 113(없음)과 그 밖의 오류를 구분해야 한다.

    2R Codex — 모든 non-zero를 '잡 없음'으로 읽으면, 일시적 오류가 bootout 성공으로
    오판돼 살아 있는 프로세스를 은퇴시킨다. 실측: 없음=113, 사용법 오류=64, 존재=0.
    """
    import subprocess as sp
    from macosctl import apply

    original = apply._launchctl
    try:
        apply._launchctl = lambda *a: sp.CompletedProcess(a, 0)
        assert apply._job_exists("com.korellas.x") is True

        apply._launchctl = lambda *a: sp.CompletedProcess(a, 113)
        assert apply._job_exists("com.korellas.x") is False, "113(없음)을 존재로 읽었다"

        # 알 수 없는 오류는 '없다'로 낙관하면 안 된다 — fail-closed
        apply._launchctl = lambda *a: sp.CompletedProcess(a, 64)
        assert apply._job_exists("com.korellas.x") is True, \
            "알 수 없는 오류를 '잡 없음'으로 낙관했다 — 살아 있는 잡을 은퇴시킨다"
    finally:
        apply._launchctl = original


def test_execute_refuses_to_run_on_corrupt_inventory():
    """손상된 인벤토리를 빈 것으로 낮추면 안 된다 (fail-closed).

    2R Codex — 폴백이 손상·읽기 실패를 {}로 강등하면, execute가 전체를 다시 쓰면서
    기존 기록을 통째로 날린다. 상위(cmd_apply)가 먼저 거르지만 안전장치는
    호출자에게 의존하면 안 된다.
    """
    from macosctl import apply

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        inv_path = tmpdir / "inv"
        inv_path.write_text("{ not json")
        try:
            apply.execute(apply.Plan(), inventory_path=inv_path,
                          daemon_dir=tmpdir, lock_path=tmpdir / "lock", state_path=tmpdir / "state",
                          apply_lock_path=tmpdir / "apply.lock",
                          log=lambda *_: None)
        except inventory.InventoryCorrupt:
            return
        raise AssertionError("손상된 인벤토리로 execute가 그냥 진행했다")


def test_apply_holds_shared_lock_while_controlling_a_service():
    """apply도 svcctl과 같은 락을 잡아야 직렬화가 성립한다.

    2R 양쪽 합의 — F5가 svcctl 쪽만 고쳐서 반쪽이었다. svc_state.locked의
    독스트링은 "apply와 svcctl이 공유한다"인데 apply는 부른 적이 없었다.
    """
    from macosctl import apply

    events = []
    original_lc = apply._launchctl

    def spy(*args):
        events.append(("launchctl", args[0] if args else ""))
        import subprocess as sp
        return sp.CompletedProcess(args, 113 if args and args[0] == "print" else 0)

    apply._launchctl = spy
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"; daemon_dir.mkdir()
            lock = tmpdir / "lock"

            from macosctl import state as state_mod
            original_locked = apply.state.locked

            from contextlib import contextmanager

            @contextmanager
            def spy_locked(path=lock):
                events.append(("lock", "acquire"))
                with original_locked(path):
                    yield
                events.append(("lock", "release"))

            apply.state.locked = spy_locked
            try:
                from macosctl import plist as plistgen
                body = plistgen.build(
                    _demo_service(), manifest.load_defaults(MANIFEST)
                )
                plan = apply.Plan(create=(apply.Unit("com.korellas.demo", "demo", body),))
                apply.execute(plan, inventory_path=tmpdir / "inv",
                              daemon_dir=daemon_dir, state_path=tmpdir / "state",
                              lock_path=lock, apply_lock_path=tmpdir / "apply.lock",
                              log=lambda *_: None)
            finally:
                apply.state.locked = original_locked
    finally:
        apply._launchctl = original_lc

    assert ("lock", "acquire") in events, "apply가 공유 락을 잡지 않았다"
    acquire = events.index(("lock", "acquire"))
    release = events.index(("lock", "release"))
    boot = [i for i, e in enumerate(events) if e == ("launchctl", "bootstrap")]
    assert boot, events
    assert acquire < boot[0] < release, (
        f"bootstrap이 락 구간 밖이다 — apply↔svcctl 경쟁이 열려 있다: {events}"
    )


def test_apply_releases_lock_between_services():
    """락은 서비스 단위로 잡았다 놓아야 한다.

    execute 전체를 감싸면 서비스당 최대 30초 bootout 대기 동안 svcctl이 블록돼
    `svc stop`이 분 단위로 멎는다 (2R Fable).
    """
    from macosctl import apply
    from contextlib import contextmanager

    events = []
    original_lc, original_locked = apply._launchctl, apply.state.locked
    apply._launchctl = _fake_launchctl([])

    @contextmanager
    def counting(path=None):
        kind = "writer" if Path(path).name == "apply.lock" else "service"
        events.append((kind, "acquire"))
        try:
            yield
        finally:
            # 진짜 state.locked는 finally로 푼다 — 유닛 하나가 실패해도 락은 풀린다.
            events.append((kind, "release"))

    apply.state.locked = counting
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"; daemon_dir.mkdir()
            from macosctl import plist as plistgen
            defaults = manifest.load_defaults(MANIFEST)
            plan = apply.Plan(create=(
                apply.Unit("com.korellas.a", "a",
                           plistgen.build(_service(name="a", label="com.korellas.a"), defaults)),
                apply.Unit("com.korellas.b", "b",
                           plistgen.build(_service(name="b", label="com.korellas.b"), defaults)),
            ))
            apply.execute(plan, inventory_path=tmpdir / "inv",
                          daemon_dir=daemon_dir, state_path=tmpdir / "state",
                          lock_path=tmpdir / "lock", apply_lock_path=tmpdir / "apply.lock",
                          log=lambda *_: None)
    finally:
        apply._launchctl = original_lc
        apply.state.locked = original_locked

    assert events.count(("service", "acquire")) == 2, (
        f"서비스 2개인데 서비스 락 획득이 잘못됐다: {events}"
    )
    assert events.count(("service", "release")) == 2, (
        f"락이 서비스마다 해제되지 않았다: {events}"
    )
    assert events == [
        ("writer", "acquire"),
        ("service", "acquire"), ("service", "release"),
        ("service", "acquire"), ("service", "release"),
        ("writer", "release"),
    ], (
        f"락 구간이 겹친다 — 서비스 단위가 아니다: {events}"
    )


def test_adopt_keeps_retire_targets_in_inventory():
    """승계가 은퇴 대상 plist를 인벤토리에서 떨어뜨리면 영구 고아가 된다.

    2R Codex — adopt는 desired(create/changed/unchanged)만 기록한다. 인벤토리가
    이미 있는 상태에서 --adopt를 돌리면 plan.retire의 plist는 설치된 채 기록에서
    빠져, 이후 어떤 apply도 그것을 은퇴시킬 수 없다 (인벤토리 밖은 안 건드린다).
    """
    from macosctl import apply

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        daemon_dir = tmpdir / "daemons"; daemon_dir.mkdir()
        (daemon_dir / "com.korellas.keep.plist").write_bytes(b"<keep/>")
        (daemon_dir / "com.korellas.gone.plist").write_bytes(b"<gone/>")

        plan = apply.Plan(
            unchanged=(apply.Unit("com.korellas.keep", "keep", b"<keep/>"),),
            retire=("com.korellas.gone",),
        )
        members = apply.adopt(plan, inventory_path=tmpdir / "inv",
                              daemon_dir=daemon_dir,
                              apply_lock_path=tmpdir / "apply.lock")
        assert "com.korellas.gone" in members, (
            f"은퇴 대상 plist가 설치돼 있는데 승계에서 빠졌다 — 영구 고아: {members}"
        )


# --- systemctl식 apply 활성화 규칙 --------------------------------------------------
#
# activate_after = was_active or (not was_boot_disabled and not was_stopped_this_boot)
#
# plist diff 경계는 그대로다 — apply는 렌더링 바이트가 달라진 유닛만 건드리고,
# 그 유닛의 적용 전 runtime/boot/현재 부팅 stop 의도를 보존한다.

BOOT_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OLD_BOOT_UUID = "00000000-1111-2222-3333-444444444444"
LABEL = "com.korellas.demo"


class ApplyFakeLaunchd:
    """launchd의 관측 가능한 사실만 흉내낸다 — job 존재/PID/disabled 오버라이드."""

    def __init__(self, *, loaded=False, pid=None, disabled=False, blind=False):
        self.loaded = loaded
        self.pid = pid
        self.disabled = disabled
        self.blind = blind
        self.calls = []
        self.next_pid = 5000

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[0] if args else ""
        if verb == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, stdout="", stderr="")
            body = f"\tpid = {self.pid}\n" if self.pid is not None else ""
            return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")
        if verb == "print-disabled":
            if self.blind:
                return subprocess.CompletedProcess(args, 5, stdout="", stderr="I/O")
            word = "disabled" if self.disabled else "enabled"
            return subprocess.CompletedProcess(
                args, 0, stdout=f'\t"{LABEL}" => {word}\n', stderr=""
            )
        if verb == "enable":
            self.disabled = False
        elif verb == "disable":
            self.disabled = True
        elif verb == "bootout":
            self.loaded, self.pid = False, None
        elif verb == "bootstrap":
            if self.disabled:
                return subprocess.CompletedProcess(
                    args, 119, stdout="", stderr="Service is disabled"
                )
            self.loaded, self.pid = True, self.next_pid
            self.next_pid += 1
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def verbs(self):
        return [call[0] for call in self.calls]


def _changed_plan(apply, daemon_dir: Path):
    svc = _demo_service()
    body = plistgen.build(svc, manifest.load_defaults(MANIFEST))
    (daemon_dir / f"{LABEL}.plist").write_bytes(b"<plist></plist>")
    return apply.Plan(changed=(apply.Unit(svc.label, svc.name, body),)), body


def _run_apply(fake, tmpdir: Path, *, boot_uuid=BOOT_UUID, state_payload=None):
    from macosctl import apply
    from macosctl import state as state_module

    daemon_dir = tmpdir / "daemons"
    daemon_dir.mkdir(exist_ok=True)
    state_path = tmpdir / "state"
    if state_payload is not None:
        state_path.write_text(json.dumps(state_payload))
    plan, body = _changed_plan(apply, daemon_dir)

    original_launchctl = apply._launchctl
    original_boot = apply._boot_session_uuid
    apply._launchctl = fake
    apply._boot_session_uuid = lambda: boot_uuid
    try:
        result = apply._execute_unlocked(
            plan,
            inventory_path=tmpdir / "inventory",
            daemon_dir=daemon_dir,
            state_path=state_path,
            known_inventory=_entries([LABEL]),
            lock_path=tmpdir / "lock",
            log=lambda *_: None,
        )
    finally:
        apply._launchctl = original_launchctl
        apply._boot_session_uuid = original_boot
    return result, body, daemon_dir, state_path


def test_activation_formula_covers_every_prior_state():
    from macosctl import apply

    def decide(active, boot_disabled, stopped):
        return apply.activate_after(apply.Observation(active, boot_disabled, stopped))

    assert decide(True, True, False) is True     # active+disabled → 유지
    assert decide(True, False, True) is True     # 실제 active가 stale intent를 이긴다
    assert decide(False, True, False) is False   # inactive+disabled → plist만 교체
    assert decide(False, False, True) is False   # 이번 부팅의 stop 의도 → 존중
    assert decide(False, False, False) is True   # inactive+enabled, 의도 없음 → 기동


def test_apply_keeps_an_active_but_boot_disabled_service_active_and_disabled():
    fake = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        result, body, daemon_dir, _ = _run_apply(fake, Path(tmp))
        assert result.failed == [], result.failed
        assert fake.pid is not None, "active+disabled 서비스를 되살리지 않았다"
        assert fake.disabled is True, "apply가 boot policy를 바꿨다"
        assert (daemon_dir / f"{LABEL}.plist").read_bytes() == body


def test_apply_replaces_the_plist_of_a_service_stopped_this_boot_without_starting_it():
    fake = ApplyFakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        result, body, daemon_dir, _ = _run_apply(
            fake, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [],
                           "stopped": {LABEL: BOOT_UUID}},
        )
        assert result.failed == []
        assert "bootstrap" not in fake.verbs(), "이번 부팅의 stop 의도를 무시했다"
        assert (daemon_dir / f"{LABEL}.plist").read_bytes() == body


def test_apply_ignores_a_stop_intent_from_a_previous_boot_session():
    fake = ApplyFakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, _ = _run_apply(
            fake, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [],
                           "stopped": {LABEL: OLD_BOOT_UUID}},
        )
        assert result.failed == []
        assert fake.pid is not None, "만료된 stop 의도가 기동을 막았다"


def test_apply_leaves_an_inactive_boot_disabled_service_inactive():
    fake = ApplyFakeLaunchd(loaded=False, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        result, body, daemon_dir, _ = _run_apply(fake, Path(tmp))
        assert result.failed == []
        assert fake.pid is None and fake.disabled is True
        assert (daemon_dir / f"{LABEL}.plist").read_bytes() == body


def test_apply_starts_an_inactive_enabled_service_with_no_stop_intent():
    fake = ApplyFakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, _ = _run_apply(fake, Path(tmp))
        assert result.failed == []
        assert fake.pid is not None


def test_apply_clears_a_stale_stop_intent_when_the_service_is_actually_active():
    fake = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, state_path = _run_apply(
            fake, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [],
                           "stopped": {LABEL: BOOT_UUID}},
        )
        assert result.failed == []
        assert json.loads(state_path.read_text())["stopped"] == {}


def test_apply_refuses_before_changing_anything_when_boot_policy_is_unobservable():
    from macosctl import apply

    fake = ApplyFakeLaunchd(loaded=False, blind=True)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _run_apply(fake, Path(tmp))
        except apply.ApplyRefused:
            assert "bootout" not in fake.verbs(), "거부 전에 이미 건드렸다"
            assert (Path(tmp) / "daemons" / f"{LABEL}.plist").read_bytes() == (
                b"<plist></plist>"
            )
            return
    raise AssertionError("boot policy를 못 봤는데도 apply가 진행했다")


def test_apply_refuses_before_changing_anything_when_state_is_corrupt():
    from macosctl import apply

    fake = ApplyFakeLaunchd(loaded=False)
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state"
        (Path(tmp) / "daemons").mkdir()
        state_path.write_text("{ broken")
        try:
            _run_apply(fake, Path(tmp))
        except apply.ApplyRefused:
            assert fake.calls == [], "손상된 state인데 launchctl을 불렀다"
            return
    raise AssertionError("손상된 state를 무시하고 apply가 진행했다")


def test_apply_refuses_before_changing_anything_when_boot_session_is_unknown():
    from macosctl import apply

    fake = ApplyFakeLaunchd(loaded=False)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "daemons").mkdir()
        try:
            _run_apply(fake, Path(tmp), boot_uuid=None)
        except apply.ApplyRefused:
            assert fake.calls == []
            return
    raise AssertionError("boot session을 모르는데 stop 의도를 판정했다")


def test_unchanged_units_are_never_touched_even_when_they_are_down():
    """plist diff 경계 — 렌더링이 같으면 런타임 장애가 있어도 apply는 손대지 않는다."""
    from macosctl import apply

    fake = ApplyFakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        daemon_dir = tmpdir / "daemons"
        daemon_dir.mkdir()
        svc = _demo_service()
        body = plistgen.build(svc, manifest.load_defaults(MANIFEST))
        (daemon_dir / f"{LABEL}.plist").write_bytes(body)
        plan = apply.Plan(unchanged=(apply.Unit(svc.label, svc.name, body),))

        original = apply._launchctl
        apply._launchctl = fake
        try:
            result = apply._execute_unlocked(
                plan, inventory_path=tmpdir / "inventory", daemon_dir=daemon_dir,
                state_path=tmpdir / "state", known_inventory=_entries([LABEL]),
                lock_path=tmpdir / "lock", log=lambda *_: None,
            )
        finally:
            apply._launchctl = original
        assert result.failed == []
        assert fake.calls == [], fake.calls



# --- 은퇴도 실제 boot policy가 정본이다 (C3a) ----------------------------------------
#
# state.disabled_by_macosctl은 "macosctl이 그렇게 만들었다"는 출처일 뿐이다.
# 실제 정책을 못 봤을 때 그것으로 대신하면, 아무도 관측하지 않은 오버라이드를 남긴
# 채 plist와 인벤토리를 지워 같은 라벨의 재등록이 119로 영구 실패하게 만든다.


def _run_retire(fake, tmpdir: Path, *, state_payload=None, boot_uuid=BOOT_UUID):
    from macosctl import apply

    daemon_dir = tmpdir / "daemons"
    daemon_dir.mkdir(exist_ok=True)
    plist = daemon_dir / f"{LABEL}.plist"
    plist.write_bytes(b"<plist/>")
    inv_path = tmpdir / "inventory"
    state_path = tmpdir / "state"
    if state_payload is not None:
        state_path.write_text(json.dumps(state_payload))
    known = _entries([LABEL])
    inventory.write(inv_path, known)

    original_launchctl = apply._launchctl
    original_boot = apply._boot_session_uuid
    apply._launchctl = fake
    apply._boot_session_uuid = lambda: boot_uuid
    try:
        result = apply._execute_unlocked(
            apply.Plan(retire=(LABEL,)),
            inventory_path=inv_path, daemon_dir=daemon_dir,
            state_path=state_path, known_inventory=known,
            lock_path=tmpdir / "lock", log=lambda *_: None,
        )
    finally:
        apply._launchctl = original_launchctl
        apply._boot_session_uuid = original_boot
    return result, plist, inv_path, state_path


def _assert_retire_preserved(plist: Path, inv_path: Path, state_path: Path):
    assert plist.exists(), "실패인데 plist를 지웠다"
    assert LABEL in (inventory.read(inv_path) or {}), "실패인데 인벤토리에서 뺐다"
    return state_path


class _BlindAfterPreflight:
    """preflight는 통과시키고 그 뒤 print-disabled만 관측 불가로 만든다."""

    def __init__(self, inner, allowed=1):
        self.inner = inner
        self.allowed = allowed
        self.seen = 0

    def __call__(self, *args):
        if args and args[0] == "print-disabled":
            self.seen += 1
            if self.seen > self.allowed:
                self.inner.calls.append(args)
                return subprocess.CompletedProcess(args, 5, stdout="", stderr="I/O")
        return self.inner(*args)


def test_retire_refuses_before_any_change_when_boot_policy_is_unobservable():
    """은퇴만 있는 계획도 첫 변경 전에 실제 정책을 확보해야 한다."""
    from macosctl import apply

    fake = ApplyFakeLaunchd(loaded=True, pid=100, blind=True)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _run_retire(fake, Path(tmp))
        except apply.ApplyRefused:
            assert "bootout" not in fake.verbs(), fake.verbs()
            assert (Path(tmp) / "daemons" / f"{LABEL}.plist").exists()
            return
    raise AssertionError("실제 boot policy를 못 봤는데도 은퇴를 진행했다")


def test_retire_reobserves_the_real_policy_immediately_before_bootout():
    """preflight 이후에 관측이 끊기면 그 라벨은 아무것도 건드리지 않고 실패한다."""
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)
    fake = _BlindAfterPreflight(inner)
    with tempfile.TemporaryDirectory() as tmp:
        result, plist, inv_path, state_path = _run_retire(fake, Path(tmp))
        assert result.retired == [], result.retired
        assert result.failed, "관측 불가를 실패로 보고하지 않았다"
        assert "bootout" not in inner.verbs(), inner.verbs()
        assert "enable" not in inner.verbs(), inner.verbs()
        _assert_retire_preserved(plist, inv_path, state_path)


def test_retire_never_falls_back_to_provenance_for_the_real_policy():
    """provenance가 있어도 실제 정책을 못 봤으면 은퇴하지 않는다 (C3a 재현)."""
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)
    fake = _BlindAfterPreflight(inner)
    with tempfile.TemporaryDirectory() as tmp:
        result, plist, inv_path, state_path = _run_retire(
            fake, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [LABEL],
                           "stopped": {}},
        )
        assert result.retired == [], "provenance를 실제 정책 대신 썼다"
        assert result.failed
        assert "bootout" not in inner.verbs(), inner.verbs()
        _assert_retire_preserved(plist, inv_path, state_path)
        assert LABEL in json.loads(
            state_path.read_text()
        )["disabled_by_macosctl"], "실패인데 provenance를 지웠다"


class _EnableThatDoesNotStick:
    """enable이 rc 0을 돌려주지만 실제로는 오버라이드가 남는다."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *args):
        if args and args[0] == "enable":
            self.inner.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return self.inner(*args)


def test_retire_verifies_the_enabled_postcondition_instead_of_the_return_code():
    """실제 disabled가 cleanup을 지배한다 — provenance가 없어도 마찬가지다."""
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)
    fake = _EnableThatDoesNotStick(inner)
    with tempfile.TemporaryDirectory() as tmp:
        result, plist, inv_path, state_path = _run_retire(fake, Path(tmp))
        assert result.retired == [], "rc 0만 보고 오버라이드를 남긴 채 지웠다"
        assert result.failed
        assert "enable" in inner.verbs(), inner.verbs()
        _assert_retire_preserved(plist, inv_path, state_path)


def test_retire_fails_when_the_enabled_postcondition_cannot_be_reobserved():
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)
    # preflight 1회 + bootout 직전 재관측 1회는 통과, 후조건 재관측만 관측 불가.
    fake = _BlindAfterPreflight(inner, allowed=2)
    with tempfile.TemporaryDirectory() as tmp:
        result, plist, inv_path, state_path = _run_retire(fake, Path(tmp))
        assert result.retired == [], result.retired
        assert result.failed
        _assert_retire_preserved(plist, inv_path, state_path)


def test_real_enabled_beats_provenance_disabled_and_retire_completes():
    """실제 enabled면 provenance가 disabled라고 해도 은퇴가 막히지 않는다."""
    fake = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        result, plist, inv_path, state_path = _run_retire(
            fake, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [LABEL],
                           "stopped": {}},
        )
        assert result.retired == [LABEL], result.failed
        assert not plist.exists(), "은퇴했는데 plist가 남았다"
        assert LABEL not in (inventory.read(inv_path) or {})
        assert "bootout" in fake.verbs()
        payload = json.loads(state_path.read_text())
        assert payload["disabled_by_macosctl"] == [], "은퇴하면서 의도를 지우지 않았다"


# --- active 보존이 intent 정리보다 앞선다 (C3b) ---------------------------------------


def test_active_service_survives_a_failing_stale_stop_intent_cleanup():
    """정리 실패가 원래 active였던 서비스를 down으로 남기면 안 된다 (C3b 재현).

    state intent는 의도이고 runtime은 사실이다. 의도 파일을 못 고쳤다고 사실을
    희생하면, apply 한 번이 살아 있던 서비스를 조용히 내려버린다.
    """
    from macosctl import apply
    from macosctl import state as state_module

    fake = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)
    original_clear = state_module.clear_stopped
    state_module.clear_stopped = lambda *a, **k: (_ for _ in ()).throw(
        OSError("state fsync")
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result, body, daemon_dir, state_path = _run_apply(
                fake, Path(tmp),
                state_payload={"version": 2, "disabled_by_macosctl": [],
                               "stopped": {LABEL: BOOT_UUID}},
            )
            assert fake.pid is not None and fake.pid != 100, (
                "정리 실패 때문에 원래 active였던 서비스를 down으로 남겼다"
            )
            assert "bootstrap" in fake.verbs(), fake.verbs()
            assert result.failed, "정리 실패를 보고하지 않았다"
            assert (daemon_dir / f"{LABEL}.plist").read_bytes() == body
    finally:
        state_module.clear_stopped = original_clear


def test_stale_stop_intent_is_kept_when_reactivation_itself_fails():
    """재활성화가 실패했으면 정지 의도를 거짓으로 지우지 않는다."""
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)

    def refuse_bootstrap(*args):
        if args and args[0] == "bootstrap":
            inner.calls.append(args)
            return subprocess.CompletedProcess(
                args, 5, stdout="", stderr="Input/output error"
            )
        return inner(*args)

    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, state_path = _run_apply(
            refuse_bootstrap, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [],
                           "stopped": {LABEL: BOOT_UUID}},
        )
        assert result.failed, "bootstrap 실패를 보고하지 않았다"
        assert json.loads(state_path.read_text())["stopped"] == {
            LABEL: BOOT_UUID
        }, "재활성화에 실패했는데 정지 의도를 지웠다"



# --- touched 활성화의 성공 정본은 실제 PID다 (C5) -------------------------------------
#
# `launchctl bootstrap`의 반환코드는 "요청이 접수됐다"는 뜻이지 "떴다"는 뜻이 아니다.
# rc를 성공으로 읽으면 apply가 아무것도 뜨지 않은 상태를 등록 성공으로 기록하고,
# 그 다음 doctor/status만이 뒤늦게 사실을 발견한다.


def test_bootstrap_return_code_does_not_substitute_for_a_real_pid():
    """rc=0인데 job/PID가 생기지 않았다 — 성공이 아니다 (C5a 재현)."""
    inner = ApplyFakeLaunchd(loaded=False, disabled=False)

    def hollow_bootstrap(*args):
        if args and args[0] == "bootstrap":
            inner.calls.append(args)  # 접수만 하고 아무것도 만들지 않는다
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return inner(*args)

    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, _ = _run_apply(hollow_bootstrap, Path(tmp))
        assert result.installed == [], result.installed
        assert result.failed, "PID 없는 bootstrap을 성공으로 기록했다"
        assert inner.pid is None


def test_unit_whose_bootout_never_finishes_is_left_completely_untouched():
    """bootout이 안 끝났으면 plist를 갈지 않는다 (C5b 재현).

    기존 job이 남은 채 plist만 바뀌면, 그 뒤에 보이는 PID가 새 등록의 것인지
    갈아치우지 못한 옛 등록의 것인지 구분할 수 없다.
    """
    from macosctl import apply

    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)
    original_boot = apply._bootout_and_wait
    apply._bootout_and_wait = lambda label: False  # 30초 타임아웃 재현
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            result, body, daemon_dir, state_path = _run_apply(inner, tmpdir)
            inv_path = tmpdir / "inventory"

            assert result.installed == [], result.installed
            assert result.failed, "bootout 미완료를 실패로 보고하지 않았다"
            # plist는 옛 바이트 그대로다 (교체 자체를 하지 않았다).
            assert (daemon_dir / f"{LABEL}.plist").read_bytes() == b"<plist></plist>"
            assert (daemon_dir / f"{LABEL}.plist").read_bytes() != body
            # inventory 항목도 갱신되지 않았다.
            assert inventory.read(inv_path)[LABEL].sha256 == "a" * 64
            # 기존 runtime은 그대로 살아 있다.
            assert inner.pid == 100
            verbs = inner.verbs()
            assert "bootstrap" not in verbs, verbs
            assert "enable" not in verbs, verbs
    finally:
        apply._bootout_and_wait = original_boot


def test_an_observed_new_pid_overrides_a_nonzero_bootstrap_return_code():
    """충돌값 대조 — rc는 실패라는데 실제로는 떴다. 실제 PID가 지배한다."""
    inner = ApplyFakeLaunchd(loaded=False, disabled=False)

    def noisy_bootstrap(*args):
        if args and args[0] == "bootstrap":
            inner(*args)  # 실제로는 등록되고 PID가 생긴다
            return subprocess.CompletedProcess(
                args, 5, stdout="", stderr="already bootstrapped"
            )
        return inner(*args)

    with tempfile.TemporaryDirectory() as tmp:
        result, body, daemon_dir, _ = _run_apply(noisy_bootstrap, Path(tmp))
        assert result.failed == [], result.failed
        assert result.installed == ["demo"], result.installed
        assert inner.pid is not None
        assert (daemon_dir / f"{LABEL}.plist").read_bytes() == body


def test_boot_policy_is_restored_even_when_the_pid_postcondition_fails():
    """임시 enable은 PID 관측 실패와 무관하게 되돌아간다 (C3 복원 의미 유지).

    active+disabled 유닛이라야 활성화 경로를 탄다 — inactive+disabled는 애초에
    기동하지 않는 것이 정상이다 (activate_after).
    """
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=True)

    def hollow_bootstrap(*args):
        if args and args[0] == "bootstrap":
            inner.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return inner(*args)

    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, _ = _run_apply(hollow_bootstrap, Path(tmp))
        assert result.installed == [], result.installed
        assert result.failed, "PID 관측 실패를 보고하지 않았다"
        assert inner.disabled is True, "임시 enable을 되돌리지 않았다"
        verbs = inner.verbs()
        assert verbs.index("enable") < verbs.index("bootstrap") < verbs.index("disable")


def test_stale_stop_intent_survives_a_failed_pid_postcondition():
    """활성화가 실제로 실패했으면 정지 의도를 지우지 않는다 (C3b 유지)."""
    inner = ApplyFakeLaunchd(loaded=True, pid=100, disabled=False)

    def hollow_bootstrap(*args):
        if args and args[0] == "bootstrap":
            inner.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return inner(*args)

    with tempfile.TemporaryDirectory() as tmp:
        result, _, _, state_path = _run_apply(
            hollow_bootstrap, Path(tmp),
            state_payload={"version": 2, "disabled_by_macosctl": [],
                           "stopped": {LABEL: BOOT_UUID}},
        )
        assert result.failed, "PID 관측 실패를 보고하지 않았다"
        assert json.loads(state_path.read_text())["stopped"] == {LABEL: BOOT_UUID}


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
