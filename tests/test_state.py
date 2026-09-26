"""svcctl desired-state 단위 테스트."""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import state  # noqa: E402

SVCCTL_PATH = REPO / "sbin" / "macosctl-helper"


BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "99999999-8888-7777-6666-555555555555"


def test_state_v2_separates_stop_intent_from_disable_provenance():
    """stopped는 boot session 한정 의도, disabled는 조작 출처다 — 같은 축이 아니다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        assert state.read(path) == state.State()

        state.record_stopped(path, "com.korellas.a", BOOT)
        state.record_disabled(path, "com.korellas.b")
        loaded = state.read(path)
        assert loaded.stopped == {"com.korellas.a": BOOT}
        assert loaded.disabled_by_macosctl == frozenset({"com.korellas.b"})

        # 서로를 지우지 않는다
        state.clear_stopped(path, "com.korellas.a")
        loaded = state.read(path)
        assert loaded.stopped == {}
        assert loaded.disabled_by_macosctl == frozenset({"com.korellas.b"})

        state.clear_disabled(path, "com.korellas.b")
        assert state.read(path) == state.State()


def test_state_v2_on_disk_shape_is_exactly_the_agreed_schema():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        state.record_stopped(path, "com.korellas.a", BOOT)
        state.record_disabled(path, "com.korellas.b")
        payload = json.loads(path.read_text())
        assert payload == {
            "version": 2,
            "disabled_by_macosctl": ["com.korellas.b"],
            "stopped": {"com.korellas.a": BOOT},
        }


def test_stop_intent_expires_when_the_boot_session_changes():
    """다음 부트에서 stopped는 자동 만료된다 — 재부팅은 RunAtLoad로 되살린다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        state.record_stopped(path, "com.korellas.a", BOOT)
        loaded = state.read(path)
        assert loaded.is_stopped_this_boot("com.korellas.a", BOOT) is True
        assert loaded.is_stopped_this_boot("com.korellas.a", OTHER_BOOT) is False
        assert loaded.is_stopped_this_boot("com.korellas.a", None) is False


def test_v1_marks_are_normalized_into_v2_on_read():
    """v1을 읽어 v2로 정규화한다 — legacy stopped는 현재 boot session에 귀속한다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 1, "marks": {
            "com.korellas.a": "stopped", "com.korellas.b": "disabled",
        }}))
        loaded = state.read(path, boot_uuid=BOOT)
        assert loaded.stopped == {"com.korellas.a": BOOT}
        assert loaded.disabled_by_macosctl == frozenset({"com.korellas.b"})


def test_v1_is_promoted_to_v2_on_the_next_write():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 1, "marks": {
            "com.korellas.a": "stopped", "com.korellas.b": "disabled",
        }}))
        state.clear_disabled(path, "com.korellas.b", boot_uuid=BOOT)
        payload = json.loads(path.read_text())
        assert payload["version"] == 2
        assert payload["disabled_by_macosctl"] == []
        assert payload["stopped"] == {"com.korellas.a": BOOT}


def test_forget_drops_both_axes_for_a_retired_label():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        state.record_stopped(path, "com.korellas.a", BOOT)
        state.record_disabled(path, "com.korellas.a")
        state.forget(path, "com.korellas.a")
        assert state.read(path) == state.State()
        state.forget(path, "com.korellas.ghost")  # 없어도 조용히 통과


def test_state_file_is_world_readable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        state.record_stopped(path, "com.korellas.a", BOOT)
        assert oct(path.stat().st_mode)[-3:] == "644"


def test_unknown_version_is_corrupt_not_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 99, "stopped": {}}))
        try:
            state.read(path)
        except state.StateCorrupt:
            return
        raise AssertionError("알 수 없는 버전을 예외 없이 읽었다")


def test_state_corrupt_file_is_not_silently_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text("{ broken")
        try:
            state.read(path)
        except state.StateCorrupt:
            return
        raise AssertionError("손상된 state를 예외 없이 읽었다")


def test_lock_primitive_is_exclusive():
    """락 원시함수 자체의 동작."""
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "lock"
        with state.locked(lock):
            assert state.try_lock_is_held(lock) is True
        assert state.try_lock_is_held(lock) is False


def test_lock_serializes_apply_and_svcctl():
    """apply와 svcctl이 **둘 다** 같은 락을 잡아야 경쟁이 막힌다.

    이 테스트는 원래 락 원시함수만 시험하고 apply가 그걸 쓰는지는 보지 않았다 —
    이름은 "직렬화한다"인데 apply를 부르지 않았다. 실제로 apply는 락을 한 번도
    잡지 않았고, 그 사실을 이 테스트가 8일간 가려줬다 (2R 양쪽 지적).
    """
    import inspect
    from macosctl import apply

    source = inspect.getsource(apply.execute) + inspect.getsource(apply._execute_unlocked)
    assert "state.locked" in source, (
        "apply.execute가 공유 락을 잡지 않는다 — svcctl만 잡으면 직렬화가 아니다"
    )

    svcctl_source = SVCCTL_PATH.read_text()
    assert "flock" in svcctl_source, "svcctl이 락을 잡지 않는다"


# --- 손상은 빈 값이 아니다 ---------------------------------------------------------
#
# state 파일은 disable provenance와 boot-scoped stop intent의 **유일한 정본**이다.
# 해석할 수 없는 값을 빈 값으로 낮추면, 그 위에서 내린 판단이 곧 정본을 덮어쓴다.
# 파일이 없는 것만 "의도가 없다"이고, 나머지는 전부 손상이다.

CORRUPT_V2_PAYLOADS = (
    {"version": 2},                                              # 필수 필드 둘 다 없음
    {"version": 2, "disabled_by_macosctl": []},                  # stopped 없음
    {"version": 2, "stopped": {}},                               # disabled 없음
    {"version": 2, "disabled_by_macosctl": [], "stopped": {},
     "extra": 1},                                                # 모르는 필드
    {"version": 2, "disabled_by_macosctl": {}, "stopped": {}},    # 리스트가 아니다
    {"version": 2, "disabled_by_macosctl": [], "stopped": []},    # 객체가 아니다
    {"version": 2, "disabled_by_macosctl": [1], "stopped": {}},   # 라벨이 문자열이 아니다
    {"version": 2, "disabled_by_macosctl": [], "stopped": {"a": 1}},  # UUID가 문자열이 아니다
)

CORRUPT_V1_PAYLOADS = (
    {"version": 1},                                              # marks 없음
    {"version": 1, "marks": {}, "extra": 1},                     # 모르는 필드
    {"version": 1, "marks": []},                                 # 객체가 아니다
    {"version": 1, "marks": {"com.korellas.a": "paused"}},       # 모르는 mark
    {"version": 1, "marks": {"com.korellas.a": 1}},              # mark가 문자열이 아니다
)


def _assert_corrupt(payload_bytes: bytes, why: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_bytes(payload_bytes)
        try:
            state.read(path, boot_uuid=BOOT)
        except state.StateCorrupt:
            return
        raise AssertionError(f"손상을 통과시켰다 ({why}): {payload_bytes!r}")


def test_v2_accepts_only_the_exact_agreed_schema():
    for payload in CORRUPT_V2_PAYLOADS:
        _assert_corrupt(json.dumps(payload).encode(), "v2")
    # 대조군: 정확한 형태는 통과한다
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({
            "version": 2,
            "disabled_by_macosctl": ["com.korellas.b"],
            "stopped": {"com.korellas.a": BOOT},
        }))
        loaded = state.read(path)
        assert loaded.disabled_by_macosctl == frozenset({"com.korellas.b"})
        assert loaded.stopped == {"com.korellas.a": BOOT}


def test_v1_accepts_only_the_exact_legacy_schema():
    for payload in CORRUPT_V1_PAYLOADS:
        _assert_corrupt(json.dumps(payload).encode(), "v1")


def test_invalid_utf8_is_corrupt_rather_than_an_unhandled_crash():
    """UnicodeDecodeError는 ValueError라 OSError 핸들러를 그냥 지나친다."""
    _assert_corrupt(b'\xff\xfe{"version": 2}', "invalid utf-8")


def test_non_object_and_unparsable_payloads_are_corrupt():
    for raw in (b"not-json", b"[]", b'"v2"', b"null", b"", b"42"):
        _assert_corrupt(raw, "non-object/unparsable")


def test_only_a_missing_file_is_an_empty_state():
    with tempfile.TemporaryDirectory() as tmp:
        assert state.read(Path(tmp) / "absent") == state.State()



# --- v1 stopped는 boot session 없이는 해석할 수 없다 ---------------------------------
#
# v1은 stop 의도가 **어느 부팅의 것이었는지** 적지 않는다. 현재 boot session에
# 귀속시켜야만 v2로 옮길 수 있고, 그 UUID를 관측할 수 없으면 내용이 깨진 것은
# 아니어도 안전하게 정규화할 수 없다. 이때 빈 의도로 낮추면, 그 판단으로 mutation을
# 끝낸 뒤 v2로 덮어쓰면서 legacy stop 의도가 조용히 사라진다.


def _without_boot_session(fn):
    original = state.boot_session_uuid
    state.boot_session_uuid = lambda: None
    try:
        return fn()
    finally:
        state.boot_session_uuid = original


def test_v1_stopped_is_unresolvable_without_an_observable_boot_session():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 1, "marks": {
            "com.korellas.legacy": "stopped",
            "com.korellas.other": "disabled",
        }}))

        def read_it():
            try:
                state.read(path)
            except state.StateCorrupt:
                return "refused"
            return "accepted"

        assert _without_boot_session(read_it) == "refused"


def test_v1_disabled_only_still_reads_without_a_boot_session():
    """disabled는 부팅과 무관한 축이다 — UUID가 없어도 해석에 문제가 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 1, "marks": {
            "com.korellas.other": "disabled",
        }}))
        loaded = _without_boot_session(lambda: state.read(path))
        assert loaded.disabled_by_macosctl == frozenset({"com.korellas.other"})
        assert loaded.stopped == {}


def test_v1_stopped_still_resolves_when_the_caller_supplies_the_uuid():
    """관측이 아니라 호출자가 UUID를 넘긴 경우는 그대로 귀속된다 (대조군)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state"
        path.write_text(json.dumps({"version": 1, "marks": {
            "com.korellas.legacy": "stopped",
        }}))
        loaded = _without_boot_session(
            lambda: state.read(path, boot_uuid=BOOT)
        )
        assert loaded.stopped == {"com.korellas.legacy": BOOT}


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
