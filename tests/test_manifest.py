"""svc manifest 단위 테스트."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import manifest  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402


def test_manifest_unfiltered_view_includes_unmanaged():
    services = manifest.load(MANIFEST)
    names = {s.name for s in services}
    # managed=false 인 항목도 status/doctor는 봐야 한다 (install-services.sh의
    # managed-only 파서와 다른 뷰다 — 설계 스펙 D1).
    assert "manual-worker" in names
    assert "worker-disabled" in names


def test_manifest_managed_view_excludes_unmanaged():
    services = manifest.load(MANIFEST)
    managed = {s.name for s in manifest.managed_only(services)}
    assert "manual-worker" not in managed
    assert "worker-disabled" not in managed
    assert "gateway" in managed


def test_manifest_parses_fields():
    services = {s.name: s for s in manifest.load(MANIFEST)}
    gateway = services["gateway"]
    assert gateway.label == "com.korellas.gateway"
    assert gateway.port == 14000
    assert gateway.managed is True
    assert gateway.depends_on == ("database", "tracing")


def test_manifest_dangling_depends_on_is_detected():
    services = manifest.load(MANIFEST)
    assert manifest.dangling_dependencies(services) == ()


def test_legacy_parser_requires_an_explicit_read_only_path():
    assert not hasattr(manifest, "DEFAULT_MANIFEST")
    for fn in (manifest.load, manifest.load_defaults):
        try:
            fn()
        except TypeError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} still has an implicit manifest path")


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
