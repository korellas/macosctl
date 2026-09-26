"""D10 apply writer/service lock tests."""

import json
import io
import hashlib
import runpy
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import apply, inventory, manifest, model, validate  # noqa: E402


def _empty_merged():
    defaults = manifest.Defaults(
        user="example", working_directory="/srv", log_dir="/tmp",
        throttle_seconds=10, path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )
    return model.MergedModel(defaults, (), ())


def _cli_args(directory: Path, *, adopt=False):
    return SimpleNamespace(
        config_root=directory, inventory=str(directory / "inventory"),
        dry_run=False, adopt=adopt,
    )


def test_apply_writer_lock_serializes_two_applies():
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    original = apply._execute_unlocked
    calls = 0

    def blocked(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(2)
        else:
            second_entered.set()
        return apply.Result()

    apply._execute_unlocked = blocked
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            kwargs = dict(
                inventory_path=d / "inv", daemon_dir=d,
                state_path=d / "state", lock_path=d / "service.lock",
                apply_lock_path=d / "apply.lock", log=lambda *_: None,
            )
            first = threading.Thread(target=apply.execute, args=(apply.Plan(),), kwargs=kwargs)
            second = threading.Thread(target=apply.execute, args=(apply.Plan(),), kwargs=kwargs)
            first.start(); assert entered.wait(2)
            second.start()
            assert not second_entered.wait(0.15), "두 번째 apply가 writer lock을 통과했다"
            release.set(); first.join(2); second.join(2)
            assert second_entered.is_set()
    finally:
        apply._execute_unlocked = original


def test_service_lock_is_held_per_service_not_globally():
    events = []
    original_locked, original_launchctl = apply.state.locked, apply._launchctl

    @contextmanager
    def locked(path):
        kind = "apply" if Path(path).name == "apply.lock" else "service"
        events.append((kind, "acquire"))
        try:
            yield
        finally:
            # 진짜 state.locked는 finally에서 flock unlock과 fd close를 한다 —
            # 유닛 하나가 실패해도 락은 풀린다. 직선으로 두면 예외가 지나갈 때
            # release가 빠져 제품이 락을 누수한 것처럼 보인다.
            events.append((kind, "release"))

    apply.state.locked = locked
    apply._launchctl = lambda *args: subprocess.CompletedProcess(
        args, 113 if args and args[0] == "print" else 0, "", ""
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); dd = d / "daemons"; dd.mkdir()
            units = (
                apply.Unit("com.korellas.a", "a", b"a"),
                apply.Unit("com.korellas.b", "b", b"b"),
            )
            original_write = apply._write_plist_atomic
            apply._write_plist_atomic = lambda path, body: path.write_bytes(body)
            try:
                apply.execute(
                    apply.Plan(create=units), inventory_path=d / "inv",
                    daemon_dir=dd, state_path=d / "state",
                    lock_path=d / "service.lock", apply_lock_path=d / "apply.lock",
                    log=lambda *_: None,
                )
            finally:
                apply._write_plist_atomic = original_write
    finally:
        apply.state.locked, apply._launchctl = original_locked, original_launchctl

    assert events == [
        ("apply", "acquire"),
        ("service", "acquire"), ("service", "release"),
        ("service", "acquire"), ("service", "release"),
        ("apply", "release"),
    ]


def test_noop_execute_migrates_v1_before_returning():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); inv = d / "inventory"
        inv.write_text(json.dumps({
            "version": 1, "labels": {"com.korellas.a": "a" * 64},
        }))
        result = apply.execute(
            apply.Plan(), inventory_path=inv, daemon_dir=d,
            state_path=d / "state", lock_path=d / "service.lock",
            apply_lock_path=d / "apply.lock", log=lambda *_: None,
        )
        assert result.exit_code() == 0
        assert json.loads(inv.read_text())["version"] == 2
        assert inv.with_name("inventory.v1.bak").exists()


def test_public_execute_rereads_inventory_inside_writer_lock():
    a = inventory.Entry("a" * 64, "a.toml", "active")
    b = inventory.Entry("b" * 64, "b.toml", "active")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); inv = d / "inventory"
        inventory.write(inv, {"com.korellas.a": a, "com.korellas.b": b})
        apply.execute(
            apply.Plan(), inventory_path=inv, daemon_dir=d,
            state_path=d / "state", known_inventory={"com.korellas.a": a},
            lock_path=d / "service.lock", apply_lock_path=d / "apply.lock",
            log=lambda *_: None,
        )
        assert inventory.read(inv) == {
            "com.korellas.a": a, "com.korellas.b": b,
        }


def test_stale_unchanged_plan_cannot_turn_current_masked_entry_active():
    masked = inventory.Entry(None, "30-ai.toml", "masked")
    stale = inventory.Entry("a" * 64, "30-ai.toml", "active")
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); inv = d / "inventory"
        inventory.write(inv, {"com.korellas.a": masked})
        original = apply._launchctl
        apply._launchctl = lambda *args: calls.append(args)
        try:
            apply.execute(
                apply.Plan(unchanged=(
                    apply.Unit("com.korellas.a", "a", b"plist", "30-ai.toml"),
                )),
                inventory_path=inv, daemon_dir=d, state_path=d / "state",
                known_inventory={"com.korellas.a": stale},
                lock_path=d / "service.lock", apply_lock_path=d / "apply.lock",
                log=lambda *_: None,
            )
        finally:
            apply._launchctl = original
        assert inventory.read(inv) == {"com.korellas.a": masked}
    assert calls == []


def test_unchanged_metadata_refresh_ignores_nonmembers_and_masked_entries():
    active = inventory.Entry("a" * 64, "a.toml", "active")
    masked = inventory.Entry(None, "b.toml", "masked")
    plan = apply.Plan(unchanged=(
        apply.Unit("com.korellas.b", "b", b"b", "new-b.toml"),
        apply.Unit("com.korellas.c", "c", b"c", "new-c.toml"),
    ))
    with tempfile.TemporaryDirectory() as tmp:
        inv = Path(tmp) / "inventory"
        inventory.write(inv, {
            "com.korellas.a": active, "com.korellas.b": masked,
        })
        members = apply._refresh_unchanged_inventory_unlocked(
            plan, inv, {"com.korellas.a": active, "com.korellas.b": masked}
        )
        assert members == {
            "com.korellas.a": active, "com.korellas.b": masked,
        }
        assert inventory.read(inv) == members


def test_cli_noop_with_missing_inventory_does_not_create_or_adopt_it():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    plan = apply.Plan(unchanged=(
        apply.Unit("com.korellas.a", "a", b"plist", "30-ai.toml"),
    ))
    renders = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); inv = d / "inventory"
        with mock.patch.object(namespace["os"], "geteuid", return_value=0), \
             mock.patch.object(namespace["confd"], "load", return_value=merged), \
             mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
             mock.patch.object(validate, "check", return_value=()), \
             mock.patch.object(validate, "has_fatal", return_value=False), \
             mock.patch.object(apply, "build_plan", return_value=plan), \
             mock.patch.object(namespace["render"], "write", side_effect=lambda _: renders.append(1)), \
             mock.patch.dict(inner.__globals__, {
                 "_read_policy_for_apply": lambda args: object(),
             }):
            with redirect_stdout(io.StringIO()):
                code = inner(_cli_args(d), apply)
        assert code == 0
        assert not inv.exists()
        assert renders == [1]


def test_public_adopt_rereads_masked_inventory_inside_writer_lock():
    a = inventory.Entry(None, "a.toml", "masked")
    b = inventory.Entry(None, "b.toml", "masked")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); dd = d / "daemons"; dd.mkdir(); inv = d / "inventory"
        inventory.write(inv, {"com.korellas.a": a, "com.korellas.b": b})
        members = apply.adopt(
            apply.Plan(), inventory_path=inv, daemon_dir=dd,
            known_inventory={"com.korellas.a": a},
            apply_lock_path=d / "apply.lock",
        )
        assert members == {"com.korellas.a": a, "com.korellas.b": b}


def test_execute_rejects_same_writer_and_service_lock_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lock"
        try:
            apply.execute(
                apply.Plan(create=(apply.Unit("com.korellas.a", "a", b"x"),)),
                inventory_path=Path(tmp) / "inventory", daemon_dir=Path(tmp),
                state_path=Path(tmp) / "state", lock_path=path,
                apply_lock_path=path,
                log=lambda *_: None,
            )
        except ValueError as exc:
            assert "락" in str(exc)
        else:
            raise AssertionError("동일 writer/service lock 경로를 허용했다")


def test_execute_rejects_symlink_alias_of_writer_lock():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); writer = d / "writer.lock"; writer.touch()
        alias = d / "service.lock"; alias.symlink_to(writer)
        try:
            apply.execute(
                apply.Plan(create=(apply.Unit("com.korellas.a", "a", b"x"),)),
                inventory_path=d / "inventory", daemon_dir=d,
                state_path=d / "state", lock_path=alias,
                apply_lock_path=writer, log=lambda *_: None,
            )
        except ValueError as exc:
            assert "락" in str(exc)
        else:
            raise AssertionError("writer lock의 symlink alias를 허용했다")


def test_cli_writer_lock_covers_load_migration_plan_execute_and_render():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    command = namespace["cmd_apply"]
    merged = _empty_merged()
    events = []

    @contextmanager
    def writer(path):
        events.append("lock-acquire")
        yield
        events.append("lock-release")

    def loaded(root):
        events.append("load")
        return merged

    def migrated(path):
        events.append("migration")
        return False

    plan = apply.Plan(create=(apply.Unit("com.korellas.a", "a", b"x"),))

    def built(*args, **kwargs):
        events.append("plan")
        return plan

    def executed(*args, **kwargs):
        events.append("execute")
        return apply.Result(installed=["a"])

    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(namespace["os"], "geteuid", return_value=0), \
         mock.patch.object(namespace["confd"], "load", side_effect=loaded), \
         mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
         mock.patch.object(validate, "check", return_value=()), \
         mock.patch.object(validate, "has_fatal", return_value=False), \
         mock.patch.object(inventory, "read", return_value={}), \
         mock.patch.object(inventory, "_migrate_unlocked", side_effect=migrated), \
         mock.patch.object(apply, "writer_locked", side_effect=writer), \
         mock.patch.object(apply, "build_plan", side_effect=built), \
         mock.patch.object(apply, "_execute_unlocked", side_effect=executed), \
         mock.patch.object(namespace["render"], "write", side_effect=lambda _: events.append("render")), \
         mock.patch.dict(command.__globals__, {
             "_read_policy_for_apply": lambda args: object(),
         }):
        with redirect_stdout(io.StringIO()):
            assert command(_cli_args(Path(tmp))) == 0

    assert events == [
        "lock-acquire", "load", "migration", "plan", "execute", "render",
        "lock-release",
    ]


def test_manifest_is_written_after_noop_and_adopt_but_not_partial_failure():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()

    def run(plan, *, adopt=False, result=None):
        writes = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(namespace["os"], "geteuid", return_value=0), \
             mock.patch.object(namespace["confd"], "load", return_value=merged), \
             mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
             mock.patch.object(validate, "check", return_value=()), \
             mock.patch.object(validate, "has_fatal", return_value=False), \
             mock.patch.object(inventory, "read", return_value={}), \
             mock.patch.object(inventory, "_migrate_unlocked", return_value=False), \
             mock.patch.object(apply, "build_plan", return_value=plan), \
             mock.patch.object(apply, "_adopt_unlocked", return_value={}), \
             mock.patch.object(apply, "_execute_unlocked", return_value=result or apply.Result()), \
             mock.patch.object(namespace["render"], "write", side_effect=lambda _: writes.append("write")), \
             mock.patch.dict(inner.__globals__, {
                 "_read_policy_for_apply": lambda args: object(),
             }):
            with redirect_stdout(io.StringIO()):
                code = inner(_cli_args(Path(tmp), adopt=adopt), apply)
        return code, writes

    assert run(apply.Plan()) == (0, ["write"])
    assert run(apply.Plan(), adopt=True) == (0, ["write"])
    failed = apply.Result(failed=[("a", "boom")])
    changed = apply.Plan(changed=(apply.Unit("com.korellas.a", "a", b"x"),))
    assert run(changed, result=failed) == (1, [])



def test_partial_failure_output_promises_no_automatic_retry():
    """부분 실패 안내가 실제 재적용 경계와 어긋나면 안 된다.

    apply의 canonical 재적용 경계는 렌더링 plist 바이트 diff다. C5의 PID 후조건에서
    실패해도 plist와 inventory SHA는 이미 desired 값으로 기록돼 있으므로, 다음
    build_plan은 그 서비스를 touched가 아니라 unchanged로 본다 — "다음 apply가
    재시도한다"는 약속은 그 경계에서 참이 아니다. 특정 명령이 모든 실패를
    해결한다고 말하는 대신, 원인 확인과 현재 상태 재진단만 안내한다.
    """
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    changed = apply.Plan(changed=(apply.Unit("com.korellas.a", "a", b"x"),))
    failed = apply.Result(failed=[("a", "bootstrap 후 PID를 확인할 수 없다")])

    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(namespace["os"], "geteuid", return_value=0), \
         mock.patch.object(namespace["confd"], "load", return_value=merged), \
         mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
         mock.patch.object(validate, "check", return_value=()), \
         mock.patch.object(validate, "has_fatal", return_value=False), \
         mock.patch.object(inventory, "read", return_value={}), \
         mock.patch.object(inventory, "_migrate_unlocked", return_value=False), \
         mock.patch.object(apply, "build_plan", return_value=changed), \
         mock.patch.object(apply, "_execute_unlocked", return_value=failed), \
         mock.patch.dict(inner.__globals__, {
             "_read_policy_for_apply": lambda args: object(),
         }):
        output = io.StringIO()
        with redirect_stdout(output):
            code = inner(_cli_args(Path(tmp)), apply)

    text = output.getvalue()
    assert code == 1, text
    assert "부분 실패" in text, text
    assert "재시도" not in text, f"거짓 자동 재시도 약속이 남아 있다: {text}"
    assert "다음 apply" not in text, f"거짓 자동 재시도 약속이 남아 있다: {text}"
    assert "다시 진단" in text, f"현재 상태 재진단 안내가 없다: {text}"


def test_cli_reports_migration_corruption_without_traceback():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(namespace["os"], "geteuid", return_value=0), \
         mock.patch.object(namespace["confd"], "load", return_value=merged), \
         mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
         mock.patch.object(validate, "check", return_value=()), \
         mock.patch.object(validate, "has_fatal", return_value=False), \
         mock.patch.object(
             inventory, "_migrate_unlocked",
             side_effect=inventory.InventoryCorrupt("broken inventory"),
         ), \
         mock.patch.dict(inner.__globals__, {
             "_read_policy_for_apply": lambda args: object(),
         }):
        output = io.StringIO()
        with redirect_stdout(output):
            code = inner(_cli_args(Path(tmp)), apply)
    assert code == 2
    assert "broken inventory" in output.getvalue()
    assert "Traceback" not in output.getvalue()


def test_cli_reports_migration_write_failure_without_traceback():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(namespace["os"], "geteuid", return_value=0), \
         mock.patch.object(namespace["confd"], "load", return_value=merged), \
         mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
         mock.patch.object(validate, "check", return_value=()), \
         mock.patch.object(validate, "has_fatal", return_value=False), \
         mock.patch.object(
             inventory, "_migrate_unlocked", side_effect=OSError("fsync failed")
         ), \
         mock.patch.dict(inner.__globals__, {
             "_read_policy_for_apply": lambda args: object(),
         }):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                code = inner(_cli_args(Path(tmp)), apply)
            except OSError as exc:
                raise AssertionError(f"migration OSError가 CLI 밖으로 샜다: {exc}")
    assert code == 1
    assert "마이그레이션" in output.getvalue()
    assert "fsync failed" in output.getvalue()
    assert "Traceback" not in output.getvalue()


def test_cli_noop_refreshes_existing_entry_source_without_launchctl():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    body = b"plist bytes"
    unit = apply.Unit("com.korellas.a", "a", body, "30-ai.toml")
    plan = apply.Plan(unchanged=(unit,))

    for legacy_v1, prior_source in ((True, None), (False, "old.toml")):
        calls, renders = [], []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); inv = d / "inventory"
            if legacy_v1:
                inv.write_text(json.dumps({
                    "version": 1,
                    "labels": {"com.korellas.a": hashlib.sha256(body).hexdigest()},
                }))
            else:
                inventory.write(inv, {
                    "com.korellas.a": inventory.Entry(
                        hashlib.sha256(body).hexdigest(), prior_source, "active"
                    )
                })
            with mock.patch.object(namespace["os"], "geteuid", return_value=0), \
                 mock.patch.object(namespace["confd"], "load", return_value=merged), \
                 mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
                 mock.patch.object(validate, "check", return_value=()), \
                 mock.patch.object(validate, "has_fatal", return_value=False), \
                 mock.patch.object(apply, "build_plan", return_value=plan), \
                 mock.patch.object(apply, "_launchctl", side_effect=lambda *a: calls.append(a)), \
                 mock.patch.object(namespace["render"], "write", side_effect=lambda _: renders.append(1)), \
                 mock.patch.dict(inner.__globals__, {
                     "_read_policy_for_apply": lambda args: object(),
                 }):
                with redirect_stdout(io.StringIO()):
                    code = inner(_cli_args(d), apply)
            entry = inventory.read(inv)["com.korellas.a"]
            assert code == 0
            assert entry == inventory.Entry(
                hashlib.sha256(body).hexdigest(), "30-ai.toml", "active"
            )
            assert calls == []
            assert renders == [1]


def test_cli_noop_inventory_refresh_failure_is_reported_and_skips_manifest():
    namespace = runpy.run_path(str(REPO / "bin" / "macosctl"))
    inner = namespace["_cmd_apply_body"]
    merged = _empty_merged()
    body = b"plist bytes"
    unit = apply.Unit("com.korellas.a", "a", body, "30-ai.toml")
    plan = apply.Plan(unchanged=(unit,))
    renders = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); inv = d / "inventory"
        inventory.write(inv, {
            "com.korellas.a": inventory.Entry(
                hashlib.sha256(body).hexdigest(), "old.toml", "active"
            )
        })
        def fail_refresh(path, entries):
            raise OSError("inventory fsync failed")

        with mock.patch.object(namespace["os"], "geteuid", return_value=0), \
             mock.patch.object(namespace["confd"], "load", return_value=merged), \
             mock.patch.object(namespace["policy"], "bind", side_effect=lambda value, _: value), \
             mock.patch.object(validate, "check", return_value=()), \
             mock.patch.object(validate, "has_fatal", return_value=False), \
             mock.patch.object(apply, "build_plan", return_value=plan), \
             mock.patch.object(inventory, "write", side_effect=fail_refresh), \
             mock.patch.object(namespace["render"], "write", side_effect=lambda _: renders.append(1)), \
             mock.patch.dict(inner.__globals__, {
                 "_read_policy_for_apply": lambda args: object(),
             }):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                try:
                    code = inner(_cli_args(d), apply)
                except OSError as exc:
                    raise AssertionError(f"noop inventory OSError가 CLI 밖으로 샜다: {exc}")
    assert code == 1
    assert "인벤토리" in output.getvalue()
    assert "inventory fsync failed" in output.getvalue()
    assert renders == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except Exception as exc:
                failures += 1; print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
