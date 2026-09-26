"""svc reconcile 계층 inventory 단위 테스트."""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import inventory  # noqa: E402
from macosctl import manifest  # noqa: E402
from macosctl import plist as plistgen  # noqa: E402
from macosctl import policy  # noqa: E402
from macosctl import validate  # noqa: E402

DAEMON_DIR = Path("/Library/LaunchDaemons")
SHA_A = "a" * 64


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


def test_inventory_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        entries = {
            "com.korellas.a": inventory.Entry(SHA_A, "30-a.toml", "active"),
            "com.korellas.b": inventory.Entry(None, "30-b.toml", "masked"),
        }
        inventory.write(path, entries)
        assert inventory.read(path) == entries


def test_inventory_missing_file_is_distinguishable_from_empty():
    """부재와 '비어 있음'은 다르다 — 부재면 은퇴 로직이 돌면 안 된다 (2R Codex #1)."""
    with tempfile.TemporaryDirectory() as tmp:
        assert inventory.read(Path(tmp) / "nope") is None
        path = Path(tmp) / "empty"
        inventory.write(path, {})
        assert inventory.read(path) == {}


def test_inventory_corrupt_file_raises_rather_than_returning_empty():
    """fail-closed — 손상된 인벤토리를 빈 것으로 읽으면 은퇴가 조용히 죽는다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        path.write_text("{ this is not json")
        try:
            inventory.read(path)
        except inventory.InventoryCorrupt:
            return
        raise AssertionError("손상된 인벤토리를 예외 없이 읽었다")


def test_inventory_is_world_readable():
    """svc status/doctor/dry-run은 일반 유저로 돈다 — 읽을 수 있어야 한다 (2R L-2).

    mkstemp 기본값이 0600이라 첫 구현에서 root만 읽을 수 있었고, 일반 유저의
    `svc apply --dry-run`이 통째로 실패했다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        inventory.write(path, {"com.korellas.a": inventory.Entry(SHA_A, None, "active")})
        assert oct(path.stat().st_mode)[-3:] == "644"


def test_inventory_permission_error_is_not_reported_as_corruption():
    """권한 부족과 손상은 다르다 — 전자는 '루트로 다시'이고 후자는 '복구 필요'다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        inventory.write(path, {"com.korellas.a": inventory.Entry(SHA_A, None, "active")})
        os.chmod(path, 0o000)
        try:
            inventory.read(path)
        except inventory.InventoryUnreadable:
            return
        except inventory.InventoryCorrupt:
            raise AssertionError("권한 문제를 손상으로 보고했다")
        finally:
            os.chmod(path, 0o644)
        raise AssertionError("권한 없는 파일을 예외 없이 읽었다")


def test_inventory_write_is_atomic_no_partial_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        inventory.write(path, {"com.korellas.a": inventory.Entry(SHA_A, None, "active")})
        leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "inventory"]
        assert leftovers == [], f"임시 파일이 남았다: {leftovers}"


def test_reads_v1_by_promoting_entries():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        path.write_text(json.dumps({
            "version": 1,
            "labels": {"com.korellas.a": SHA_A},
        }))

        assert inventory.read(path) == {
            "com.korellas.a": inventory.Entry(SHA_A, None, "active")
        }


def test_unknown_version_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        path.write_text(json.dumps({"version": 3, "labels": {}}))

        try:
            inventory.read(path)
        except inventory.InventoryCorrupt as exc:
            assert "버전" in str(exc)
            return
        raise AssertionError("모르는 인벤토리 버전을 읽었다")


def test_migrate_writes_backup_before_replacing():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "inventory"
        lock_path = directory / "apply.lock"
        original = (json.dumps({
            "version": 1,
            "labels": {"com.korellas.a": SHA_A},
        }, indent=2) + "\n").encode()
        path.write_bytes(original)

        real_replace = inventory.os.replace
        saw_backup = []

        def observing_replace(source, destination):
            if Path(destination) == path:
                backup = path.with_name(path.name + inventory.BACKUP_SUFFIX)
                saw_backup.append(backup.exists() and backup.read_bytes() == original)
            return real_replace(source, destination)

        inventory.os.replace = observing_replace
        try:
            assert inventory.migrate(path, lock_path) is True
        finally:
            inventory.os.replace = real_replace

        backup = path.with_name(path.name + inventory.BACKUP_SUFFIX)
        assert saw_backup == [True], "v1 백업이 내구화되기 전에 원본을 교체했다"
        assert backup.read_bytes() == original
        assert inventory.read(path) == {
            "com.korellas.a": inventory.Entry(SHA_A, None, "active")
        }
        assert json.loads(path.read_text())["version"] == 2


def test_migrate_is_idempotent_on_v2():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "inventory"
        entry = inventory.Entry(SHA_A, "30-a.toml", "active")
        inventory.write(path, {"com.korellas.a": entry})
        before = path.read_bytes()

        assert inventory.migrate(path, directory / "apply.lock") is False
        assert path.read_bytes() == before
        assert not path.with_name(path.name + inventory.BACKUP_SUFFIX).exists()


def test_write_is_atomic_and_world_readable():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "inventory"
        inventory.write(path, {
            "com.korellas.a": inventory.Entry(SHA_A, "30-a.toml", "active")
        })

        assert path.stat().st_mode & 0o777 == 0o644
        assert json.loads(path.read_text()) == {
            "version": 2,
            "labels": {
                "com.korellas.a": {
                    "sha256": SHA_A,
                    "source": "30-a.toml",
                    "lifecycle": "active",
                }
            },
        }
        assert [p for p in directory.iterdir() if p.name.startswith(".inventory-")] == []


def test_masked_entry_has_null_sha256():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        inventory.write(path, {
            "com.korellas.masked": inventory.Entry(None, "30-mask.toml", "masked")
        })

        raw = json.loads(path.read_text())
        assert raw["labels"]["com.korellas.masked"]["sha256"] is None
        assert inventory.read(path)["com.korellas.masked"] == inventory.Entry(
            None, "30-mask.toml", "masked"
        )


def test_invalid_utf8_is_reported_as_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        path.write_bytes(b"\xff")

        try:
            inventory.read(path)
        except inventory.InventoryCorrupt as exc:
            assert "손상" in str(exc)
            return
        raise AssertionError("invalid UTF-8 인벤토리를 손상으로 거부하지 않았다")


def test_active_sha256_is_lowercase_64_hex_for_entry_read_and_write():
    invalid_hashes = ("short", "A" * 64, "g" * 64)
    for digest in invalid_hashes:
        try:
            inventory.Entry(digest, None, "active")
        except ValueError:
            pass
        else:
            raise AssertionError(f"Entry가 잘못된 sha256을 받았다: {digest!r}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        try:
            inventory.write(path, {"com.korellas.a": "a" * 64})
        except TypeError:
            pass
        else:
            raise AssertionError("write가 v1 문자열 항목을 계속 허용한다")

        for version, value in (
            (1, "short"),
            (2, {"sha256": "A" * 64, "source": None, "lifecycle": "active"}),
        ):
            path.write_text(json.dumps({
                "version": version,
                "labels": {"com.korellas.a": value},
            }))
            try:
                inventory.read(path)
            except inventory.InventoryCorrupt:
                pass
            else:
                raise AssertionError(f"v{version} read가 잘못된 sha256을 받았다")


def test_label_uses_engine_policy_grammar_on_read_and_write():
    assert inventory.LABEL_PATTERN.pattern == policy.LABEL_PATTERN.pattern
    bad_label = "/tmp/victim"
    entry = inventory.Entry(SHA_A, None, "active")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        try:
            inventory.write(path, {bad_label: entry})
        except ValueError:
            pass
        else:
            raise AssertionError("write가 경로형 label을 받았다")

        path.write_text(json.dumps({
            "version": 1,
            "labels": {bad_label: SHA_A},
        }))
        try:
            inventory.read(path)
        except inventory.InventoryCorrupt:
            return
        raise AssertionError("read가 경로형 label을 받았다")


def test_unlocked_migration_core_does_not_reacquire_apply_lock():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inventory"
        path.write_text(json.dumps({
            "version": 1,
            "labels": {"com.korellas.a": SHA_A},
        }))

        original_locked = inventory.state.locked
        inventory.state.locked = lambda *_: (_ for _ in ()).throw(
            AssertionError("외부 apply.lock 안에서 락을 재획득했다")
        )
        try:
            assert inventory._migrate_unlocked(path) is True
        finally:
            inventory.state.locked = original_locked

        assert inventory.read(path) == {
            "com.korellas.a": inventory.Entry(SHA_A, None, "active")
        }

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
