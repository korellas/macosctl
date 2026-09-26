"""svc 관측 계층(manifest / collect / doctor) 단위 테스트."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import collect  # noqa: E402
from macosctl import doctor  # noqa: E402
from macosctl import manifest  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "drift-synthetic"


def test_parse_listeners_handles_fixture_and_raw_lsof():
    text = (FIXTURE / "lsof-listen.txt").read_text()
    listeners = collect.parse_listeners(text)
    by_port = {}
    for l in listeners:
        by_port.setdefault(l.port, []).append(l)
    assert by_port[15432][0].pid == 101
    assert by_port[14000][0].pid == 202
    assert by_port[18001][0].pid == 201
    # 리스너 없던 포트는 아예 등장하지 않는다
    assert 18000 not in by_port
    assert 13003 not in by_port


def test_parse_listeners_dedupes_ipv4_ipv6_same_pid():
    # postgres는 IPv4/IPv6 두 줄로 잡히지만 한 프로세스다 (오탐 방지)
    text = (FIXTURE / "lsof-listen.txt").read_text()
    rows = [l for l in collect.parse_listeners(text) if l.port == 15432]
    assert len(rows) == 1


def test_parse_jobs_reads_state_and_pid():
    text = (FIXTURE / "launchctl-print.txt").read_text()
    jobs = collect.parse_jobs(text)
    assert jobs["com.korellas.gateway"].pid == 103
    assert jobs["com.korellas.gateway"].state == "running"
    assert jobs["com.korellas.worker-b"].loaded is False
    # 중첩된 endpoint의 'state = active'를 job state로 오독하면 안 된다
    assert jobs["com.korellas.database"].state == "running"


def test_parse_installed_labels():
    text = (FIXTURE / "installed-plists.txt").read_text()
    labels = collect.parse_installed_labels(text)
    assert "com.korellas.gateway" in labels
    assert "com.webtop" in labels
    assert "com.korellas.worker-b" not in labels


def test_parse_ppids():
    text = (FIXTURE / "processes.txt").read_text()
    ppids = collect.parse_ppids(text)
    assert ppids[201] == 200
    assert ppids[103] == 1


def test_parse_disabled_overrides():
    text = (FIXTURE / "print-disabled.txt").read_text()
    overrides = collect.parse_disabled_overrides(text)
    assert overrides["com.korellas.retired-a"] is False  # enabled
    assert "com.korellas.retired-d" in overrides


def test_collect_live_populates_operator_intent():
    """collect_live가 의도를 채워야 stopped-by-operator가 죽은 코드를 면한다.

    SystemState의 의도 필드는 한때 "Phase 3(D6)에서 채운다"는 주석만 달린 채
    비어 있었고, 그래서 라이브 doctor에서 D7-3 진단이 통째로 죽어 있었다.
    """
    import inspect
    source = inspect.getsource(collect.collect_live)
    assert "_operator_intent" in source, (
        "collect_live가 의도를 채우지 않는다 — stopped-by-operator가 죽은 코드다"
    )


def test_system_state_carries_the_two_intent_axes_separately():
    """관측 스냅샷은 '이번 부팅의 정지 의도'와 'macosctl이 disable했다'를 나눠 든다."""
    state = collect.SystemState(
        installed_labels=frozenset(), jobs={}, listeners=(), ppids={},
        disabled_overrides={},
        stopped_this_boot=frozenset({"com.korellas.a"}),
        disabled_by_macosctl=frozenset({"com.korellas.b"}),
    )
    assert state.stopped_this_boot == frozenset({"com.korellas.a"})
    assert state.disabled_by_macosctl == frozenset({"com.korellas.b"})


def test_collect_live_scopes_stop_intent_to_the_current_boot_session():
    """다른 부팅에서 남은 정지 의도는 관측 스냅샷에 들어오면 안 된다."""
    import inspect
    source = inspect.getsource(collect._operator_intent)
    assert "stopped_this_boot" in source and "boot_session_uuid" in source, (
        "collect가 정지 의도를 boot session으로 한정하지 않는다"
    )


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
