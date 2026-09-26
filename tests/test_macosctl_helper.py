"""helper root 래퍼 단위 테스트."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import state  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402

SVCCTL_PATH = REPO / "sbin" / "macosctl-helper"


def _write_inventory(path: Path, labels: dict, *, version: int = 1):
    path.write_text(json.dumps({"version": version, "labels": labels}))


def _load_helper():
    # 확장자가 없어 기본 finder가 로더를 못 고른다 — 소스 로더를 명시한다.
    loader = importlib.machinery.SourceFileLoader(
        "helper_under_test", str(SVCCTL_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_helper_does_not_import_from_user_writable_repo():
    """자기완결형이어야 한다 — 레포 모듈을 import하면 root 실행 경로가 오염된다."""
    source = SVCCTL_PATH.read_text()
    assert "sys.path.insert" not in source, "레포 경로를 sys.path에 넣고 있다"
    for module in ("manifest", "svc_collect", "svc_doctor", "svc_apply",
                   "svc_inventory", "svc_validate", "svc_state"):
        assert f"import {module}" not in source, f"{module}을 import하고 있다"


def test_helper_reads_no_user_writable_path():
    """레포 모듈 import 금지를 '사용자 쓰기 가능 경로 읽기 금지'로 넓힌다."""
    source = SVCCTL_PATH.read_text()
    for forbidden in (
        "/etc/macosctl/conf.d", "/etc/macosctl/macosctl.toml", "services.toml", "/Users/"
    ):
        assert forbidden not in source, f"{forbidden}을 읽고 있다"
    assert "/etc/macosctl/policy.json" in source, "정책 경로가 없다"


def test_helper_never_uses_shell():
    """셸 문자열 조합 금지 — argv 배열로만 exec한다 (2R Codex #5)."""
    source = SVCCTL_PATH.read_text()
    assert "shell=True" not in source
    assert "os.system" not in source


def test_rejects_unknown_verb():
    helper = _load_helper()
    assert helper.resolve_verb("delete") is None
    assert helper.resolve_verb("stop") is not None


def test_rejects_injection_in_name():
    helper = _load_helper()
    policy = helper.Policy("com.korellas.", {"webtop": "com.webtop"})
    for bad in ("a; rm -rf /", "../../etc/passwd", "a b", "com.apple.sshd",
                "UPPER", "x" * 41, "", "a/b"):
        assert helper.resolve_label(bad, policy) is None, f"허용하면 안 되는 이름: {bad!r}"


def test_resolves_label_from_name():
    helper = _load_helper()
    policy = helper.Policy("org.example.", {"webtop": "com.webtop"})
    assert helper.resolve_label("worker-b", policy) == "org.example.worker-b"
    assert helper.resolve_label("webtop", policy) == "com.webtop"


def test_read_policy_ignores_engine_keys_and_uses_one_relative_open():
    """helper은 label mapping만 읽고 검사한 fd를 그대로 사용한다 (D2-2·3)."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "etc-svc"
        root.mkdir(mode=0o755)
        path = root / "policy.json"
        path.write_text(json.dumps({
            "schema": 1,
            "label_prefix": "org.example.",
            "label_exceptions": {"webtop": "com.webtop"},
            # 엔진 전용 키는 helper 재배포 없이 늘어날 수 있어야 한다.
            "service_user": "somebody",
            "groups": ["infra"],
            "future_engine_key": {"nested": True},
        }))
        path.chmod(0o644)

        calls = []
        original_open = helper.os.open

        def recording_open(name, flags, mode=0o777, *, dir_fd=None):
            calls.append((name, flags, dir_fd))
            return original_open(name, flags, mode, dir_fd=dir_fd)

        helper.os.open = recording_open
        try:
            policy = helper._read_policy(
                path,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
        finally:
            helper.os.open = original_open

        assert policy.label_prefix == "org.example."
        assert policy.label_exceptions == {"webtop": "com.webtop"}
        policy_opens = [call for call in calls if call[0] == "policy.json"]
        assert len(policy_opens) == 1, calls
        assert policy_opens[0][2] is not None, calls


def test_read_policy_fails_closed_on_missing_or_malformed_mapping():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        path = root / "policy.json"
        invalid = (
            {"schema": 1, "label_exceptions": {}},
            {"schema": 1, "label_prefix": "../", "label_exceptions": {}},
            {"schema": 1, "label_prefix": "com.example.", "label_exceptions": []},
            {"schema": 1, "label_prefix": "com.example.",
             "label_exceptions": {"demo": "../../bad"}},
        )
        for value in invalid:
            path.write_text(json.dumps(value))
            path.chmod(0o644)
            try:
                helper._read_policy(
                    path,
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                )
            except helper.PolicyRefused:
                pass
            else:
                raise AssertionError(f"잘못된 정책을 허용했다: {value!r}")


def test_read_policy_refuses_insecure_file_directory_and_symlink():
    helper = _load_helper()
    valid = json.dumps({
        "schema": 1,
        "label_prefix": "com.example.",
        "label_exceptions": {},
    })
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        path = root / "policy.json"
        path.write_text(valid)
        path.chmod(0o666)

        def refused(target=path):
            try:
                helper._read_policy(
                    target,
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                )
            except helper.PolicyRefused:
                return
            raise AssertionError(f"안전하지 않은 정책을 허용했다: {target}")

        refused()
        path.chmod(0o644)
        root.chmod(0o775)
        refused()
        root.chmod(0o755)
        path.unlink()
        real = root / "real-policy.json"
        real.write_text(valid)
        real.chmod(0o644)
        path.symlink_to(real)
        refused()


def test_read_policy_refuses_multiply_linked_or_oversized_file():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        path = root / "policy.json"
        path.write_text(json.dumps({
            "schema": 1,
            "label_prefix": "com.example.",
            "label_exceptions": {},
        }))
        path.chmod(0o644)
        hardlink = root / "policy-copy.json"
        os.link(path, hardlink)
        try:
            helper._read_policy(
                path, expected_uid=os.getuid(), expected_gid=os.getgid()
            )
        except helper.PolicyRefused:
            pass
        else:
            raise AssertionError("hard-linked policy를 허용했다")
        hardlink.unlink()
        path.write_bytes(b" " * (helper.MAX_POLICY_BYTES + 1))
        try:
            helper._read_policy(
                path, expected_uid=os.getuid(), expected_gid=os.getgid()
            )
        except helper.PolicyRefused:
            pass
        else:
            raise AssertionError("크기 상한을 넘긴 policy를 허용했다")


def test_authorize_requires_inventory_membership():
    """root 소유 plist 존재만으로는 부족하다 (2R Codex #5).

    root 소유는 '누가 설치했는지'를 증명하지 않는다. 다른 root 절차가 설치한
    com.korellas.* 도 제어 대상이 되면 'apply가 설치한 것만 제어'라는 경계가
    root ownership 하나로 무너진다.
    """
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        plist = tmpdir / "com.korellas.demo.plist"
        plist.write_bytes(b"<plist/>")
        digest = helper.sha256_file(plist)
        inv = tmpdir / "inventory"

        _write_inventory(inv, {"com.korellas.demo": digest})
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is None

        # 인벤토리 밖 — root 소유 plist가 있어도 거부
        _write_inventory(inv, {})
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is not None


def test_authorize_reads_both_inventory_versions():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        plist = tmpdir / "com.korellas.demo.plist"
        plist.write_bytes(b"<plist/>")
        digest = helper.sha256_file(plist)
        inv = tmpdir / "inventory"

        _write_inventory(inv, {"com.korellas.demo": digest}, version=1)
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is None

        _write_inventory(inv, {"com.korellas.demo": {
            "sha256": digest,
            "lifecycle": "active",
        }}, version=2)
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is None


def test_helper_ignores_v2_source_and_extra_metadata():
    """D7-7: helper의 인가 투영은 membership·sha256·lifecycle뿐이다."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        plist = d / "com.korellas.demo.plist"
        plist.write_bytes(b"<plist/>")
        digest = helper.sha256_file(plist)
        inv = d / "inventory"
        _write_inventory(inv, {"com.korellas.demo": {
            "sha256": digest,
            "lifecycle": "active",
            "source": {"not": "helper data"},
            "future_engine_metadata": [1, 2, 3],
        }}, version=2)
        assert helper.authorize("com.korellas.demo", d, inv) is None
        entry, denial = helper._inventory_entry("com.korellas.demo", inv)
        assert denial is None
        assert entry == {"sha256": digest, "lifecycle": "active"}

        inv.unlink()
        _write_inventory(inv, {"com.korellas.m": {
            "sha256": None,
            "lifecycle": "masked",
            "source": ["also", "ignored"],
            "future_engine_metadata": {"anything": True},
        }}, version=2)
        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 113, stdout="", stderr="Could not find service"
        )
        assert helper.authorize_masked(
            "com.korellas.m", d, inv, verb="stop"
        ) is None
        entry, denial = helper._inventory_entry("com.korellas.m", inv)
        assert denial is None
        assert entry == {"sha256": None, "lifecycle": "masked"}


def test_authorize_rejects_unknown_or_corrupt_inventory():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        plist = tmpdir / "com.korellas.demo.plist"
        plist.write_bytes(b"<plist/>")
        digest = helper.sha256_file(plist)
        inv = tmpdir / "inventory"
        invalid = (
            {"version": 99, "labels": {"com.korellas.demo": digest}},
            {"version": 2, "labels": {"com.korellas.demo": {
                "sha256": digest, "source": None, "lifecycle": "mystery"}}},
            {"version": 2, "labels": {"com.korellas.demo": {
                "sha256": None, "source": None, "lifecycle": "active"}}},
            {"version": 2, "labels": {"com.korellas.demo": {
                "sha256": digest, "source": None, "lifecycle": "masked"}}},
        )
        for value in invalid:
            inv.write_text(json.dumps(value))
            assert helper.authorize("com.korellas.demo", tmpdir, inv) is not None


def _masked_inventory(path: Path, *, sha256=None, lifecycle="masked"):
    _write_inventory(path, {"com.korellas.m": {
        "sha256": sha256,
        "lifecycle": lifecycle,
    }}, version=2)


def test_masked_refuses_start_restart_enable():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inv = d / "inventory"
        _masked_inventory(inv)
        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 113, stdout="", stderr="Could not find service"
        )
        for verb in ("start", "restart", "enable"):
            denial = helper.authorize_masked(
                "com.korellas.m", d, inv, verb=verb
            )
            assert denial and verb in denial


def test_masked_allows_stop_and_disable():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inv = d / "inventory"
        _masked_inventory(inv)
        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 113, stdout="", stderr="Could not find service"
        )
        for verb in ("stop", "disable"):
            assert helper.authorize_masked(
                "com.korellas.m", d, inv, verb=verb
            ) is None


def test_masked_refuses_when_plist_or_job_is_present():
    """masked인데 plist나 job이 있으면 svc가 설치하지 않은 대상일 수 있다."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inv = d / "inventory"
        _masked_inventory(inv)
        absent = lambda *args: subprocess.CompletedProcess(  # noqa: E731
            args, 113, stdout="", stderr="Could not find service"
        )
        helper._launchctl = absent
        assert helper.authorize_masked(
            "com.korellas.m", d, inv, verb="stop"
        ) is None

        (d / "com.korellas.m.plist").write_bytes(b"<plist/>")
        denial = helper.authorize_masked(
            "com.korellas.m", d, inv, verb="stop"
        )
        assert denial and "plist" in denial
        (d / "com.korellas.m.plist").unlink()

        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 0, stdout="service = {}", stderr=""
        )
        denial = helper.authorize_masked(
            "com.korellas.m", d, inv, verb="stop"
        )
        assert denial and "job" in denial


def test_masked_refuses_when_job_absence_cannot_be_established():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inv = d / "inventory"
        _masked_inventory(inv)
        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 5, stdout="", stderr="I/O error"
        )
        denial = helper.authorize_masked(
            "com.korellas.m", d, inv, verb="disable"
        )
        assert denial and "확인" in denial

        def launchctl_failure(*args):
            raise OSError("launchctl unavailable")

        helper._launchctl = launchctl_failure
        denial = helper.authorize_masked(
            "com.korellas.m", d, inv, verb="disable"
        )
        assert denial and "확인" in denial


def test_masked_refuses_malformed_sha_or_lifecycle():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inv = d / "inventory"
        helper._launchctl = lambda *args: subprocess.CompletedProcess(
            args, 113, stdout="", stderr="Could not find service"
        )
        for sha256, lifecycle in (("0" * 64, "masked"), (None, "active")):
            _masked_inventory(inv, sha256=sha256, lifecycle=lifecycle)
            assert helper.authorize_masked(
                "com.korellas.m", d, inv, verb="stop"
            ) is not None


def test_perform_masked_stop_and_disable_never_bootout():
    helper = _load_helper()
    calls = []
    marks = []
    label = "com.korellas.m"

    def launchctl(*args):
        calls.append(args)
        if args and args[0] == "print-disabled":
            return subprocess.CompletedProcess(
                args, 0, stdout=f'\t"{label}" => disabled\n', stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as tmp:
        helper.STATE = Path(tmp) / "state"  # preflight가 실제 /var/db를 읽지 않도록
        helper._launchctl = launchctl
        helper._boot_session_uuid = lambda: "boot-uuid"
        helper._record_stopped = lambda label, boot_uuid: marks.append(
            (label, "stopped")
        )
        helper._record_disabled_provenance = lambda label: marks.append(
            (label, "disabled")
        )

        code, _ = helper.perform_masked("stop", label)
        assert code == 0
        assert calls == [], calls
        assert marks == [(label, "stopped")]

        marks.clear()
        code, _ = helper.perform_masked("disable", label)
        assert code == 0
        # disable 자체와 **읽기 전용** 후조건 재관측 둘뿐이다 — bootout은 없다.
        assert calls == [
            ("disable", f"system/{label}"), ("print-disabled", "system")
        ], calls
        assert marks == [(label, "disabled")]


def test_perform_masked_disable_failure_does_not_record_disabled():
    helper = _load_helper()
    calls = []
    marks = []

    def launchctl(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 5, stdout="", stderr="I/O error")

    label = "com.korellas.m"

    with tempfile.TemporaryDirectory() as tmp:
        helper.STATE = Path(tmp) / "state"  # preflight가 실제 /var/db를 읽지 않도록
        helper._launchctl = launchctl
        helper._record_disabled_provenance = lambda label: marks.append(
            (label, "disabled")
        )

        code, message = helper.perform_masked("disable", label)
        assert code == 1 and "I/O error" in message
        assert calls == [("disable", f"system/{label}")], calls
        assert marks == []


def test_authorize_rejects_hash_mismatch():
    """plist가 인벤토리 기록 이후 바뀌었으면 거부한다."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        plist = tmpdir / "com.korellas.demo.plist"
        plist.write_bytes(b"<plist/>")
        inv = tmpdir / "inventory"
        _write_inventory(inv, {"com.korellas.demo": "0" * 64})
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is not None


def test_authorize_rejects_symlink():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        real = tmpdir / "real.plist"
        real.write_bytes(b"<plist/>")
        link = tmpdir / "com.korellas.demo.plist"
        link.symlink_to(real)
        inv = tmpdir / "inventory"
        _write_inventory(inv, {"com.korellas.demo": helper.sha256_file(real)})
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is not None


def test_authorize_rejects_missing_plist():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        inv = tmpdir / "inventory"
        _write_inventory(inv, {"com.korellas.demo": "0" * 64})
        assert helper.authorize("com.korellas.demo", tmpdir, inv) is not None


def test_authorize_rejects_when_inventory_missing():
    """인벤토리가 없으면 아무것도 제어하지 않는다 (fail-closed)."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "com.korellas.demo.plist").write_bytes(b"<plist/>")
        assert helper.authorize("com.korellas.demo", tmpdir,
                                tmpdir / "nope") is not None


def test_stopped_service_is_excluded_from_bootstrap():
    """이번 부팅의 정지 의도는 plist만 갈고 bootstrap을 생략한다 (2R Fable H-2).

    'enable·kickstart만 건너뛴다'는 v2의 서술은 틀렸다 — 생성 plist가
    RunAtLoad+KeepAlive라 bootstrap 하는 순간 프로세스가 뜬다.
    """
    from macosctl import apply

    def decide(active, boot_disabled, stopped):
        return apply.activate_after(apply.Observation(active, boot_disabled, stopped))

    assert decide(False, False, False) is True
    assert decide(False, False, True) is False
    assert decide(False, True, False) is False


def test_apply_does_not_revive_stopped_service_when_plist_changes():
    """정지된 서비스의 plist가 바뀌어도 apply는 되살리지 않는다 (Phase 3 완료 기준).

    """
    from macosctl import apply
    from macosctl import plist as plistgen
    from macosctl import manifest

    calls: list[tuple] = []

    def fake_launchctl(*args):
        calls.append(args)
        import subprocess as sp
        # print=113(없음). 실측 반환코드 — 임의의 non-zero는 fail-closed로 폴링된다.
        rc = 113 if args and args[0] == "print" else 0
        return sp.CompletedProcess(args, rc, stdout="", stderr="")

    original = apply._launchctl
    apply._launchctl = fake_launchctl
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daemon_dir = tmpdir / "daemons"
            daemon_dir.mkdir()
            state_path = tmpdir / "state"
            inv_path = tmpdir / "inventory"

            svc = manifest.Service(
                name="demo", label="com.korellas.demo", port=9999, group="t",
                managed=True, exec_argv=("/bin/echo", "hi"), depends_on=(),
                mem_budget=None, env=(),
            )
            defaults = manifest.load_defaults(MANIFEST)
            body = plistgen.build(svc, defaults)

            # 설치본을 다르게 만들어 '변경'으로 판정되게 한다
            (daemon_dir / "com.korellas.demo.plist").write_bytes(b"<plist></plist>")

            plan = apply.Plan(
                changed=(apply.Unit(label=svc.label, name=svc.name, body=body),)
            )

            # 이번 부팅의 정지 의도가 있는 상태
            boot_uuid = state.boot_session_uuid()
            state.record_stopped(state_path, svc.label, boot_uuid)
            apply.execute(plan, inventory_path=inv_path, daemon_dir=daemon_dir,
                          lock_path=tmpdir / "lock", state_path=state_path,
                          apply_lock_path=tmpdir / "apply.lock", log=lambda *_: None)

            bootstraps = [c for c in calls if c and c[0] == "bootstrap"]
            assert not bootstraps, f"정지된 서비스를 bootstrap했다: {bootstraps}"
            # plist는 갱신돼야 한다 (다음 start가 새 정의로 뜨도록)
            assert (daemon_dir / "com.korellas.demo.plist").read_bytes() == body

            # 의도가 없으면 정상적으로 bootstrap 한다 (대조군)
            calls.clear()
            state.clear_stopped(state_path, svc.label)
            (daemon_dir / "com.korellas.demo.plist").write_bytes(b"<plist></plist>")
            apply.execute(plan, inventory_path=inv_path, daemon_dir=daemon_dir,
                          lock_path=tmpdir / "lock", state_path=state_path,
                          apply_lock_path=tmpdir / "apply.lock", log=lambda *_: None)
            assert [c for c in calls if c and c[0] == "bootstrap"], \
                "마크가 없는데도 bootstrap하지 않았다"
    finally:
        apply._launchctl = original


def test_helper_authorizes_inside_the_lock():
    """인가는 락 안에서 해야 한다 — 락을 기다리는 동안 apply가 plist를 갈면
    인가 근거(해시)와 실제 실행 대상이 어긋난다.

    지금은 authorize가 락 밖이고 그 뒤에 락을 잡는다.
    """
    helper = _load_helper()
    held = {}

    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "lock"
        helper.LOCK = lock
        helper.audit = lambda message: None
        helper._launchctl = lambda *a: None
        helper.read_policy = lambda: helper.Policy("com.korellas.", {})

        def spy_inventory(label, inventory):
            # 자기 자신이 잡은 락은 같은 프로세스에서 재확인할 수 없으므로,
            # 락 파일이 열려 있는지(= 잠금 구간 진입 여부)를 표식으로 본다.
            held["locked"] = lock.exists()
            return {"sha256": "0" * 64, "source": None,
                    "lifecycle": "active"}, None

        helper._inventory_entry = spy_inventory
        helper._authorize_active_entry = lambda label, daemon_dir, entry: None
        helper.perform = lambda verb, label, force, **kwargs: (0, "ok")
        helper.main(["macosctl-helper", "start", "demo"])

    assert held.get("locked") is True, (
        "authorize가 락 획득 전에 호출됐다 — read-then-act 경쟁이 열려 있다"
    )


def test_main_refuses_invalid_policy_before_lock_or_inventory_read():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "lock"
        helper.LOCK = lock

        def invalid_policy():
            raise helper.PolicyRefused("invalid policy")

        helper.read_policy = invalid_policy
        helper._inventory_entry = lambda *args: (_ for _ in ()).throw(
            AssertionError("정책 거부 뒤에 인벤토리를 읽었다")
        )
        assert helper.main(["macosctl-helper", "stop", "demo"]) == 3
        assert not lock.exists()


def test_main_dispatches_masked_through_the_separate_branch():
    helper = _load_helper()
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        helper.LOCK = Path(tmp) / "lock"
        helper.read_policy = lambda: helper.Policy("com.korellas.", {})
        helper.audit = lambda message: None
        helper._inventory_entry = lambda label, inventory: ({
            "sha256": None, "source": "30-ai.toml", "lifecycle": "masked",
        }, None)
        helper._authorize_masked_entry = lambda *args, **kwargs: None
        helper.perform = lambda *args: (_ for _ in ()).throw(
            AssertionError("masked가 active perform으로 들어갔다")
        )
        helper.perform_masked = lambda verb, label, **kwargs: calls.append(
            (verb, label)
        ) or (0, "ok")

        assert helper.main(["macosctl-helper", "disable", "demo"]) == 0

    assert calls == [("disable", "com.korellas.demo")]


# --- systemctl식 의미론 (start/stop = runtime, enable/disable = boot policy) -------

BOOT_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeLaunchd:
    """launchd의 관측 가능한 사실만 흉내낸다 — job 존재/PID/disabled 오버라이드."""

    def __init__(self, *, loaded=False, pid=None, disabled=False):
        self.loaded = loaded
        self.pid = pid
        self.disabled = disabled
        self.calls = []
        self.next_pid = 4242

    def launchctl(self, *args):
        self.calls.append(args)
        verb = args[0] if args else ""
        if verb == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(
                    args, 113, stdout="", stderr="Could not find service"
                )
            body = f"\tpid = {self.pid}\n" if self.pid is not None else ""
            return subprocess.CompletedProcess(
                args, 0, stdout=f"service = {{\n{body}}}\n", stderr=""
            )
        if verb == "print-disabled":
            word = "disabled" if self.disabled else "enabled"
            return subprocess.CompletedProcess(
                args, 0, stdout=f'\t"com.korellas.demo" => {word}\n', stderr=""
            )
        if verb == "enable":
            self.disabled = False
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if verb == "disable":
            self.disabled = True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if verb == "bootout":
            self.loaded, self.pid = False, None
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if verb == "bootstrap":
            if self.disabled:
                return subprocess.CompletedProcess(
                    args, 119, stdout="",
                    stderr="Bootstrap failed: 119: Service is disabled",
                )
            self.loaded, self.pid = True, self.next_pid
            self.next_pid += 1
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if verb == "kickstart":
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, stdout="", stderr="")
            self.pid = self.next_pid
            self.next_pid += 1
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def verbs(self):
        return [call[0] for call in self.calls]


def _wire(helper, fake, tmp, *, boot_uuid=BOOT_UUID):
    """helper을 fake launchd + 임시 state에 묶는다."""
    helper._launchctl = fake.launchctl
    helper._boot_session_uuid = lambda: boot_uuid
    helper.STATE = Path(tmp) / "state"
    helper.DAEMON_DIR = Path(tmp)
    (Path(tmp) / "com.korellas.demo.plist").write_bytes(b"<plist/>")
    helper.ACTIVATION_TIMEOUT_SECONDS = 2
    helper.ABSENCE_TIMEOUT_SECONDS = 2
    helper.POLL_INTERVAL_SECONDS = 0.01
    helper.TERM_GRACE_SECONDS = 0

    def terminate(pid):
        fake.pid = None
        if fake.loaded:  # KeepAlive가 되살린다
            fake.pid = fake.next_pid
            fake.next_pid += 1
        return True, ""

    helper._terminate_process = terminate
    return helper


def _read_state(helper):
    return json.loads(Path(helper.STATE).read_text())


def test_start_preserves_boot_disabled_policy():
    """start는 runtime만 바꾼다 — boot-disabled 서비스도 active+disabled로 만든다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("start", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.pid is not None, "기동하지 못했다"
        assert fake.disabled is True, "start가 boot policy를 바꿨다"
        verbs = fake.verbs()
        assert verbs.index("enable") < verbs.index("bootstrap") < verbs.index("disable")


def test_start_is_idempotent_and_clears_only_the_stop_intent():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=999, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        helper._record_stopped("com.korellas.demo", BOOT_UUID)
        helper._record_disabled_provenance("com.korellas.demo")
        code, _ = helper.perform("start", "com.korellas.demo", force=False)
        assert code == 0
        assert "bootstrap" not in fake.verbs(), "이미 active인데 재등록했다"
        payload = _read_state(helper)
        assert payload["stopped"] == {}
        assert payload["disabled_by_macosctl"] == ["com.korellas.demo"]


def test_start_fails_closed_when_boot_policy_cannot_be_observed():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)

        def blind(*args):
            if args and args[0] == "print-disabled":
                return subprocess.CompletedProcess(args, 5, stdout="", stderr="I/O")
            return fake.launchctl(*args)

        helper._launchctl = blind
        code, message = helper.perform("start", "com.korellas.demo", force=False)
        assert code == 1 and "boot" in message
        assert "bootstrap" not in fake.verbs()


def test_stop_preserves_boot_policy_and_records_boot_scoped_intent():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=777, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        helper._record_disabled_provenance("com.korellas.other")
        code, _ = helper.perform("stop", "com.korellas.demo", force=False)
        assert code == 0
        assert fake.loaded is False
        assert fake.disabled is False, "stop이 boot policy를 바꿨다"
        payload = _read_state(helper)
        assert payload["stopped"] == {"com.korellas.demo": BOOT_UUID}
        assert payload["disabled_by_macosctl"] == ["com.korellas.other"]


def test_stop_does_not_record_intent_when_the_job_never_goes_away():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=777)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)

        def stubborn(*args):
            if args and args[0] == "bootout":
                fake.calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return fake.launchctl(*args)

        helper._launchctl = stubborn
        code, message = helper.perform("stop", "com.korellas.demo", force=False)
        assert code == 1, message
        assert not Path(helper.STATE).exists(), "실패한 정지를 의도로 기록했다"


def test_enable_changes_only_boot_policy_and_keeps_runtime_and_stop_intent():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        helper._record_stopped("com.korellas.demo", BOOT_UUID)
        helper._record_disabled_provenance("com.korellas.demo")
        code, _ = helper.perform("enable", "com.korellas.demo", force=False)
        assert code == 0
        assert fake.disabled is False
        assert fake.pid == 555, "enable이 runtime을 건드렸다"
        assert "bootout" not in fake.verbs() and "bootstrap" not in fake.verbs()
        payload = _read_state(helper)
        assert payload["disabled_by_macosctl"] == []
        assert payload["stopped"] == {"com.korellas.demo": BOOT_UUID}


def test_disable_changes_only_boot_policy_and_records_provenance():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, _ = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 0
        assert fake.disabled is True
        assert fake.pid == 555, "disable이 runtime을 정지시켰다"
        assert "bootout" not in fake.verbs(), "disable이 bootout했다"
        assert _read_state(helper)["disabled_by_macosctl"] == ["com.korellas.demo"]


def test_disable_verifies_the_postcondition_instead_of_the_return_code():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)

        def lying(*args):
            if args and args[0] == "disable":
                fake.calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return fake.launchctl(*args)

        helper._launchctl = lying
        code, message = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 1, message
        assert not Path(helper.STATE).exists(), "실패한 disable을 provenance로 기록했다"


def test_restart_activates_an_inactive_service():
    """restart는 inactive도 최종 active로 만든다 (systemctl restart와 같다)."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        helper._record_stopped("com.korellas.demo", BOOT_UUID)
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.pid is not None
        assert _read_state(helper)["stopped"] == {}


def test_restart_promotes_to_recreate_when_the_job_has_no_pid():
    """loaded-but-no-PID는 kill/kickstart로 낫지 않는다 — bootout→bootstrap으로 승격."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=None, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.pid is not None
        verbs = fake.verbs()
        assert "bootout" in verbs and "bootstrap" in verbs, verbs


def test_restart_returns_a_replacement_pid_for_a_running_service():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=100, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.pid != 100, "새 PID를 확인하지 않았다"
        assert "bootout" not in fake.verbs(), "정상 재기동인데 재등록까지 했다"


def test_restart_falls_back_to_recreate_when_no_replacement_pid_appears():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=100, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        helper._terminate_process = lambda pid: (True, "")  # 되살아나지 않는다
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 0, message
        assert "bootout" in fake.verbs(), "재기동 실패인데 승격하지 않았다"
        assert fake.pid is not None


def test_restart_preserves_boot_disabled_policy():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=None, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.pid is not None and fake.disabled is True


def test_recreate_reregisters_from_the_start():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=100, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform(
            "restart", "com.korellas.demo", force=False, recreate=True
        )
        assert code == 0, message
        verbs = fake.verbs()
        assert "bootout" in verbs and "bootstrap" in verbs
        assert fake.pid != 100


def test_force_and_recreate_are_mutually_exclusive():
    helper = _load_helper()
    assert helper.main(
        ["macosctl-helper", "restart", "demo", "--force", "--recreate"]
    ) == 2


def test_enable_never_composes_a_start_even_for_an_inactive_service():
    """합성이 가장 탐나는 자리 — 내려가 있고 boot도 막힌 서비스.

    여기서 helper이 알아서 기동해 주면 root 경계가 UX를 따라 넓어진다. enable은
    boot policy 한 축만 옮기고, 기동 여부는 CLI가 별도 호출로 정한다.
    """
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("enable", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.disabled is False
        assert fake.loaded is False and fake.pid is None, "enable이 기동까지 했다"
        assert "bootstrap" not in fake.verbs(), fake.verbs()


def test_disable_never_records_a_stop_intent():
    """정지 의도를 남기는 것은 stop뿐이다 — disable이 그것까지 하면 합성이다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=321, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 0, message
        assert fake.loaded is True and fake.pid == 321, "disable이 정지까지 했다"
        payload = _read_state(helper)
        assert payload["disabled_by_macosctl"] == ["com.korellas.demo"]
        assert payload["stopped"] == {}, "disable이 정지 의도를 남겼다"


def test_masked_disable_records_only_the_disable_intent():
    """masked도 같은 경계다 — 두 낮추는 의도는 두 번의 호출로 얻는다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        code, message = helper.perform_masked("disable", "com.korellas.demo")
        assert code == 0, message
        payload = _read_state(helper)
        assert payload["disabled_by_macosctl"] == ["com.korellas.demo"]
        assert payload["stopped"] == {}, "masked disable이 정지 의도까지 남겼다"
        assert "bootout" not in fake.verbs(), "masked인데 bootout했다"

        # 나머지 절반은 별도 호출로 온다 (CLI가 --now에서 이 둘을 순서대로 부른다).
        assert helper.perform_masked("stop", "com.korellas.demo")[0] == 0
        assert _read_state(helper)["stopped"] == {"com.korellas.demo": BOOT_UUID}


def test_helper_state_writer_uses_the_v2_schema():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=1)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        Path(helper.STATE).write_text(json.dumps({
            "version": 1, "marks": {"com.korellas.legacy": "disabled"},
        }))
        helper._record_stopped("com.korellas.demo", BOOT_UUID)
        payload = _read_state(helper)
        assert payload["version"] == 2
        assert payload["disabled_by_macosctl"] == ["com.korellas.legacy"]
        assert payload["stopped"] == {"com.korellas.demo": BOOT_UUID}



def test_helper_refuses_the_now_flag_entirely():
    """복합 UX는 helper의 일이 아니다 — root 경계는 단일 동작 하나로 유지한다."""
    helper = _load_helper()
    for verb in ("enable", "disable", "start", "stop", "restart"):
        assert helper.main(["macosctl-helper", verb, "demo", "--now"]) == 2, verb
    assert "--now" not in helper.USAGE, helper.USAGE
    assert "--now" not in helper.FLAGS, helper.FLAGS


def test_helper_verbs_take_a_single_action_each():
    """perform/perform_masked에 합성 인자가 없어야 경계가 코드로 고정된다."""
    import inspect

    helper = _load_helper()
    for fn in (helper.perform, helper.perform_masked):
        assert "now" not in inspect.signature(fn).parameters, fn.__name__



# --- 손상된 state 위에서는 아무것도 하지 않는다 --------------------------------------
#
# helper은 root로 돌면서 launchd와 state 정본을 동시에 만진다. 그 둘 사이의 판단
# 근거(state)를 읽지 못했는데 실행을 계속하면, 실제 변경을 끝낸 뒤 해석하지 못한
# 정본을 "빈 것"으로 덮어쓴다 — 그 순간 무엇이 의도였는지 복구할 길이 사라진다.

CORRUPT_STATE = b"not-json"


def _corrupt(helper, raw: bytes = CORRUPT_STATE) -> bytes:
    Path(helper.STATE).write_bytes(raw)
    return raw


def test_disable_on_corrupt_state_changes_nothing():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        original = _corrupt(helper)
        code, message = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 1, message
        assert fake.calls == [], f"손상된 state인데 launchctl을 불렀다: {fake.calls}"
        assert fake.disabled is False
        assert Path(helper.STATE).read_bytes() == original, "손상 원본을 덮어썼다"


def test_start_on_corrupt_state_changes_nothing():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        original = _corrupt(helper)
        code, message = helper.perform("start", "com.korellas.demo", force=False)
        assert code == 1, message
        assert fake.calls == [], fake.calls
        assert fake.pid is None
        assert Path(helper.STATE).read_bytes() == original


def test_stop_on_corrupt_state_changes_nothing():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        original = _corrupt(helper)
        code, message = helper.perform("stop", "com.korellas.demo", force=False)
        assert code == 1, message
        assert fake.calls == [], fake.calls
        assert fake.loaded is True, "손상된 state인데 정지시켰다"
        assert Path(helper.STATE).read_bytes() == original


def test_restart_on_corrupt_state_changes_nothing():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        original = _corrupt(helper)
        code, message = helper.perform("restart", "com.korellas.demo", force=False)
        assert code == 1, message
        assert fake.calls == [], fake.calls
        assert fake.pid == 555
        assert Path(helper.STATE).read_bytes() == original


def test_masked_verbs_on_corrupt_state_change_nothing():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        for verb in ("stop", "disable"):
            original = _corrupt(helper)
            code, message = helper.perform_masked(verb, "com.korellas.demo")
            assert code == 1, f"{verb}: {message}"
            assert fake.calls == [], f"{verb}: {fake.calls}"
            assert Path(helper.STATE).read_bytes() == original


def test_helper_parser_rejects_the_same_shapes_the_package_rejects():
    """helper은 자기완결형이지만 v1/v2 의미는 패키지와 같아야 한다."""
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        helper.STATE = Path(tmp) / "state"
        for payload in (
            b"not-json",
            b"[]",
            b'\xff\xfe{"version": 2}',
            json.dumps({"version": 2}).encode(),
            json.dumps({"version": 2, "disabled_by_macosctl": []}).encode(),
            json.dumps({"version": 2, "disabled_by_macosctl": [], "stopped": {},
                        "extra": 1}).encode(),
            json.dumps({"version": 2, "disabled_by_macosctl": [1],
                        "stopped": {}}).encode(),
            json.dumps({"version": 2, "disabled_by_macosctl": [],
                        "stopped": {"a": 1}}).encode(),
            json.dumps({"version": 1}).encode(),
            json.dumps({"version": 1, "marks": {"a": "paused"}}).encode(),
            json.dumps({"version": 3, "disabled_by_macosctl": [],
                        "stopped": {}}).encode(),
        ):
            helper.STATE.write_bytes(payload)
            try:
                helper._read_state()
            except helper.StateCorrupt:
                continue
            raise AssertionError(f"helper이 손상을 통과시켰다: {payload!r}")

        # 파일 없음만 빈 의도다.
        helper.STATE.unlink()
        assert helper._read_state() == (set(), {})


def test_stop_never_bootouts_without_a_boot_session_uuid():
    """정지 의도를 기록할 수 없으면 runtime을 건드리지 않는다.

    bootout 뒤에 UUID를 읽으면, 못 읽었을 때 이미 내려간 상태가 된다 — 다음
    apply가 의도 없음으로 보고 다시 띄우는 부분 실패다.
    """
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp, boot_uuid=None)
        code, message = helper.perform("stop", "com.korellas.demo", force=False)
        assert code == 1, message
        assert "bootout" not in fake.verbs(), fake.verbs()
        assert fake.loaded is True and fake.pid == 555


def test_masked_stop_records_nothing_without_a_boot_session_uuid():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp, boot_uuid=None)
        code, message = helper.perform_masked("stop", "com.korellas.demo")
        assert code == 1, message
        assert not Path(helper.STATE).exists(), "UUID도 없이 정지 의도를 기록했다"


def test_stop_records_the_intent_under_the_uuid_it_preflighted():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        seen = []

        def once():
            seen.append(len(seen))
            # 두 번째 관측이 달라져도 기록은 preflight한 UUID여야 한다.
            return BOOT_UUID if not seen[:-1] else "cccccccc-dddd-eeee-ffff-000000000000"

        helper._boot_session_uuid = once
        code, message = helper.perform("stop", "com.korellas.demo", force=False)
        assert code == 0, message
        assert _read_state(helper)["stopped"] == {"com.korellas.demo": BOOT_UUID}


# --- masked disable도 후조건으로 판정한다 (C2b) --------------------------------------


def test_masked_disable_verifies_the_postcondition_instead_of_the_return_code():
    """실제 boot policy 정본은 print-disabled다 — rc 0은 성공의 증거가 아니다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)

        def lying(*args):
            if args and args[0] == "disable":
                fake.calls.append(args)  # rc 0이지만 실제로는 안 바뀐다
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return fake.launchctl(*args)

        helper._launchctl = lying
        code, message = helper.perform_masked("disable", "com.korellas.demo")
        assert code == 1, message
        assert fake.disabled is False
        assert not Path(helper.STATE).exists(), "후조건 미달인데 provenance를 남겼다"


def test_masked_disable_refuses_when_boot_policy_cannot_be_reobserved():
    helper = _load_helper()
    fake = FakeLaunchd(loaded=False, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)

        def blind(*args):
            if args and args[0] == "print-disabled":
                return subprocess.CompletedProcess(args, 5, stdout="", stderr="I/O")
            return fake.launchctl(*args)

        helper._launchctl = blind
        code, message = helper.perform_masked("disable", "com.korellas.demo")
        assert code == 1, message
        assert not Path(helper.STATE).exists(), "재확인 못 했는데 provenance를 남겼다"



LEGACY_STOPPED_STATE = json.dumps({
    "version": 1, "marks": {"com.korellas.legacy": "stopped"},
}).encode()


def test_v1_stopped_without_a_boot_session_blocks_every_mutating_verb():
    """legacy stop 의도를 현재 부팅에 귀속시킬 수 없으면 아무것도 바꾸지 않는다.

    빈 의도로 낮추면 disable 하나가 v2 승격을 트리거하면서 legacy stop 의도를
    통째로 지워버린다 — 내용이 깨진 적도 없는데.
    """
    for verb, fake in (
        ("disable", FakeLaunchd(loaded=True, pid=555, disabled=False)),
        ("start", FakeLaunchd(loaded=False, disabled=False)),
        ("stop", FakeLaunchd(loaded=True, pid=555)),
    ):
        helper = _load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            _wire(helper, fake, tmp, boot_uuid=None)
            Path(helper.STATE).write_bytes(LEGACY_STOPPED_STATE)
            code, message = helper.perform(verb, "com.korellas.demo", force=False)
            assert code == 1, f"{verb}: {message}"
            assert fake.calls == [], f"{verb}: {fake.calls}"
            assert Path(helper.STATE).read_bytes() == LEGACY_STOPPED_STATE, (
                f"{verb}: legacy 의도를 덮어썼다"
            )


def test_v1_stopped_without_a_boot_session_blocks_masked_verbs_too():
    for verb in ("stop", "disable"):
        helper = _load_helper()
        fake = FakeLaunchd(loaded=False, disabled=False)
        with tempfile.TemporaryDirectory() as tmp:
            _wire(helper, fake, tmp, boot_uuid=None)
            Path(helper.STATE).write_bytes(LEGACY_STOPPED_STATE)
            code, message = helper.perform_masked(verb, "com.korellas.demo")
            assert code == 1, f"{verb}: {message}"
            assert fake.calls == [], f"{verb}: {fake.calls}"
            assert Path(helper.STATE).read_bytes() == LEGACY_STOPPED_STATE


def test_helper_reads_v1_disabled_only_without_a_boot_session():
    """disabled 축은 부팅과 무관하다 — UUID 없이도 그대로 읽힌다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp, boot_uuid=None)
        Path(helper.STATE).write_bytes(json.dumps({
            "version": 1, "marks": {"com.korellas.other": "disabled"},
        }).encode())
        assert helper._read_state() == ({"com.korellas.other"}, {})

        # 그래서 disable은 UUID 없이도 정상 진행하고 provenance만 승격된다.
        code, message = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 0, message
        payload = _read_state(helper)
        assert payload["version"] == 2
        assert payload["disabled_by_macosctl"] == [
            "com.korellas.demo", "com.korellas.other",
        ]
        assert payload["stopped"] == {}


def test_v1_stopped_survives_promotion_when_the_boot_session_is_observable():
    """대조군 — UUID가 보이면 legacy 의도는 v2로 그대로 옮겨진다."""
    helper = _load_helper()
    fake = FakeLaunchd(loaded=True, pid=555, disabled=False)
    with tempfile.TemporaryDirectory() as tmp:
        _wire(helper, fake, tmp)
        Path(helper.STATE).write_bytes(LEGACY_STOPPED_STATE)
        code, message = helper.perform("disable", "com.korellas.demo", force=False)
        assert code == 0, message
        payload = _read_state(helper)
        assert payload["stopped"] == {"com.korellas.legacy": BOOT_UUID}
        assert payload["disabled_by_macosctl"] == ["com.korellas.demo"]


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
