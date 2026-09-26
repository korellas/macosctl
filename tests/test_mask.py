"""D5 apply mask semantics."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import apply as apply_mod  # noqa: E402
from macosctl import inventory, manifest, model, state  # noqa: E402


SHA = "a" * 64


def _fake_launchctl(calls):
    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 113 if args and args[0] == "print" else 0, "", ""
        )
    return fake


def _merged(service_lifecycle="masked"):
    defaults = manifest.Defaults(
        user="example", working_directory="/srv", log_dir="/tmp",
        throttle_seconds=10, path="/usr/bin:/bin",
        log_rotate_interval_seconds=900,
    )
    service = model.MergedService(
        name="m", label="com.korellas.m", port=9999, group="test",
        managed=False, exec_argv=("/bin/true",), depends_on=(),
        mem_budget=None, env=(), working_directory="/srv",
        lifecycle=service_lifecycle,
        sources=(model.Provenance("name", "30-ai.toml"),),
    )
    return model.MergedModel(defaults, (service,), ())


def test_mask_preserves_operator_intent_and_identity():
    calls = []
    original = apply_mod._launchctl
    apply_mod._launchctl = _fake_launchctl(calls)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); dd = d / "daemons"; dd.mkdir()
            (dd / "com.korellas.m.plist").write_bytes(b"<plist/>")
            inv, st = d / "inv", d / "state"
            inventory.write(inv, {
                "com.korellas.m": inventory.Entry(SHA, "30-ai.toml", "active")
            })
            state.record_disabled(st, "com.korellas.m")

            apply_mod.execute(
                apply_mod.Plan(mask=("com.korellas.m",)),
                inventory_path=inv, daemon_dir=dd, state_path=st,
                lock_path=d / "lock", apply_lock_path=d / "alock",
                log=lambda *_: None,
            )

            assert "com.korellas.m" in state.read(st).disabled_by_macosctl
            entry = inventory.read(inv)["com.korellas.m"]
            assert entry.lifecycle == "masked" and entry.sha256 is None
            assert not (dd / "com.korellas.m.plist").exists()
    finally:
        apply_mod._launchctl = original

    assert ("enable", "system/com.korellas.m") not in calls


def test_masked_fragment_unlinked_transitions_to_retire():
    merged = model.MergedModel(_merged().defaults, (), ())
    known = {
        "com.korellas.m": inventory.Entry(None, "30-ai.toml", "masked")
    }
    with tempfile.TemporaryDirectory() as tmp:
        plan = apply_mod.build_plan(
            merged, merged.defaults, REPO, known, Path(tmp),
            config_root=Path("/etc/macosctl"),
        )
    assert plan.mask == ()
    assert plan.retire == ("com.korellas.m",)


def test_new_mask_without_inventory_membership_is_not_touched():
    merged = _merged()
    with tempfile.TemporaryDirectory() as tmp:
        plan = apply_mod.build_plan(
            merged, merged.defaults, REPO, {}, Path(tmp),
            config_root=Path("/etc/macosctl"),
        )
    assert plan.mask == () and plan.retire == ()


def test_unmasked_active_service_with_masked_inventory_is_touched():
    merged = _merged("active")
    active = model.MergedService(
        **{**merged.services[0].__dict__, "managed": True, "lifecycle": "active"}
    )
    merged = model.MergedModel(merged.defaults, (active,), ())
    known = {"com.korellas.m": inventory.Entry(None, "30-ai.toml", "masked")}
    with tempfile.TemporaryDirectory() as tmp:
        dd = Path(tmp)
        unit = apply_mod.desired_units(
            merged.services, merged.defaults, REPO, Path("/etc/macosctl")
        )[0]
        (dd / "com.korellas.m.plist").write_bytes(unit.body)
        plan = apply_mod.build_plan(
            merged, merged.defaults, REPO, known, dd,
            config_root=Path("/etc/macosctl"),
        )
    assert [unit.label for unit in plan.changed] == ["com.korellas.m"]
    assert plan.unchanged == (), "masked inventory를 active/no-op으로 오판했다"


def test_adopt_keeps_installed_mask_target_owned_for_next_apply():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); dd = d / "daemons"; dd.mkdir()
        target = dd / "com.korellas.m.plist"; target.write_bytes(b"<plist/>")
        previous = inventory.Entry(SHA, "30-ai.toml", "active")
        members = apply_mod.adopt(
            apply_mod.Plan(mask=("com.korellas.m",)),
            inventory_path=d / "inv", daemon_dir=dd,
            known_inventory={"com.korellas.m": previous},
            apply_lock_path=d / "apply.lock",
        )
        assert members["com.korellas.m"].lifecycle == "active"
        assert members["com.korellas.m"].source == "30-ai.toml"


def test_adopt_preserves_stably_masked_identity_without_a_plist():
    masked = inventory.Entry(None, "30-ai.toml", "masked")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); dd = d / "daemons"; dd.mkdir()
        members = apply_mod.adopt(
            apply_mod.Plan(), inventory_path=d / "inv", daemon_dir=dd,
            known_inventory={"com.korellas.m": masked},
            apply_lock_path=d / "apply.lock",
        )
        assert members == {"com.korellas.m": masked}


def test_adopt_preserves_pending_mask_identity_when_plist_is_already_absent():
    active = inventory.Entry(SHA, "30-ai.toml", "active")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); dd = d / "daemons"; dd.mkdir()
        members = apply_mod.adopt(
            apply_mod.Plan(mask=("com.korellas.m",)),
            inventory_path=d / "inv", daemon_dir=dd,
            known_inventory={"com.korellas.m": active},
            apply_lock_path=d / "apply.lock",
        )
        assert members == {"com.korellas.m": active}


def test_retire_logs_when_it_restores_a_disabled_override():
    calls, logs = [], []
    original_launchctl = apply_mod._launchctl

    # 오버라이드는 실제 launchd처럼 enable로 뒤집힌다. 고정된 disabled를 계속
    # 돌려주면 "enable이 결코 먹히지 않는 launchd"를 모델링하게 되고, 그러면 은퇴가
    # 후조건 미확인으로 멈춰 복원 경고 자체에 도달하지 못한다.
    disabled = {"value": True}

    def fake(*args):
        calls.append(args)
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 113, "", "")
        if args[0] == "enable":
            disabled["value"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "print-disabled":
            word = "disabled" if disabled["value"] else "enabled"
            return subprocess.CompletedProcess(
                args, 0, f'"com.korellas.m" => {word}\n', ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    apply_mod._launchctl = fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); dd = d / "daemons"; dd.mkdir()
            (dd / "com.korellas.m.plist").write_bytes(b"x")
            inv, st = d / "inv", d / "state"
            inventory.write(inv, {
                "com.korellas.m": inventory.Entry(SHA, "30-ai.toml", "active")
            })
            apply_mod.execute(
                apply_mod.Plan(retire=("com.korellas.m",)),
                inventory_path=inv, daemon_dir=dd, state_path=st,
                lock_path=d / "lock", apply_lock_path=d / "apply.lock",
                log=logs.append,
            )
    finally:
        apply_mod._launchctl = original_launchctl

    assert any("disabled 오버라이드를 복원" in line for line in logs), logs


def test_mask_updates_source_from_current_merged_provenance():
    merged = _merged()
    for prior_source in (None, "old.toml"):
        known = {
            "com.korellas.m": inventory.Entry(None, prior_source, "masked")
        }
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); dd = d / "daemons"; dd.mkdir()
            plan = apply_mod.build_plan(
                merged, merged.defaults, REPO, known, dd,
                config_root=Path("/etc/macosctl"),
            )
            assert plan.mask == ("com.korellas.m",)
            assert plan.mask_sources == (("com.korellas.m", "30-ai.toml"),)
            apply_mod.execute(
                apply_mod.Plan(mask=plan.mask, mask_sources=plan.mask_sources),
                inventory_path=d / "inv", daemon_dir=dd,
                state_path=d / "state", known_inventory=known,
                lock_path=d / "lock", apply_lock_path=d / "apply.lock",
                log=lambda *_: None,
            )
            assert inventory.read(d / "inv")["com.korellas.m"].source == "30-ai.toml"


def test_stale_mask_or_retire_plan_never_touches_foreign_plist():
    calls = []
    original = apply_mod._launchctl
    apply_mod._launchctl = _fake_launchctl(calls)
    try:
        for category in ("mask", "retire"):
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp); dd = d / "daemons"; dd.mkdir()
                target = dd / "com.korellas.m.plist"; target.write_bytes(b"foreign")
                inv = d / "inv"; inventory.write(inv, {})
                stale = inventory.Entry(SHA, "old.toml", "active")
                plan = apply_mod.Plan(**{category: ("com.korellas.m",)})
                result = apply_mod.execute(
                    plan, inventory_path=inv, daemon_dir=dd,
                    state_path=d / "state",
                    known_inventory={"com.korellas.m": stale},
                    lock_path=d / "lock", apply_lock_path=d / "apply.lock",
                    log=lambda *_: None,
                )
                assert result.failed
                assert target.read_bytes() == b"foreign"
    finally:
        apply_mod._launchctl = original
    assert calls == [], "현재 inventory 비멤버를 launchctl로 건드렸다"


def test_retire_keeps_identity_when_state_clear_fails():
    calls = []
    original_launchctl, original_clear = apply_mod._launchctl, apply_mod.state.forget
    apply_mod._launchctl = _fake_launchctl(calls)
    apply_mod.state.forget = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state fsync"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); dd = d / "daemons"; dd.mkdir()
            (dd / "com.korellas.m.plist").write_bytes(b"x")
            inv = d / "inv"
            entry = inventory.Entry(SHA, "30-ai.toml", "active")
            inventory.write(inv, {"com.korellas.m": entry})
            result = apply_mod.execute(
                apply_mod.Plan(retire=("com.korellas.m",)),
                inventory_path=inv, daemon_dir=dd, state_path=d / "state",
                lock_path=d / "lock", apply_lock_path=d / "apply.lock",
                log=lambda *_: None,
            )
            assert result.failed
            assert inventory.read(inv)["com.korellas.m"] == entry
    finally:
        apply_mod._launchctl, apply_mod.state.forget = original_launchctl, original_clear


def test_apply_output_distinguishes_retire_from_mask():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        (root / "conf.d").mkdir()
        (root / "macosctl.toml").write_text(
            'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
        )
        (root / "conf.d" / "30-ai.toml").write_text(
            'schema=1\n[[service]]\nname="m"\nlabel="com.korellas.m"\nport=9999\n'
            'group="test"\nexec=["/usr/bin/true"]\n'
        )
        dropin = root / "conf.d" / "m.d"; dropin.mkdir()
        (dropin / "90-mask.toml").write_text(
            "schema = 1\nmanaged = false\n"
        )
        (root / "policy.json").write_text(json.dumps({
            "schema": 1, "label_prefix": "com.korellas.",
            "label_exceptions": {}, "service_user": "example",
            "groups": ["test"],
        }))
        (root / "policy.json").chmod(0o644)
        inv = root / "inventory"
        inventory.write(inv, {
            "com.korellas.m": inventory.Entry(SHA, "30-ai.toml", "active"),
            "com.korellas.gone": inventory.Entry(SHA, "old.toml", "active"),
        })
        before = {
            str(path.relative_to(root)): (path.is_dir(), None if path.is_dir() else path.read_bytes())
            for path in root.rglob("*")
        }
        result = subprocess.run(
            [sys.executable, str(REPO / "bin" / "macosctl"), "--config-root",
             str(root), "apply", "--dry-run", "--inventory", str(inv)],
            capture_output=True, text=True, check=False,
        )
        after = {
            str(path.relative_to(root)): (path.is_dir(), None if path.is_dir() else path.read_bytes())
            for path in root.rglob("*")
        }
    assert result.returncode == 0, result.stdout + result.stderr
    assert after == before, "dry-run이 staging 파일/목록을 변경했다"
    assert "- 은퇴" in result.stdout and "com.korellas.gone" in result.stdout
    assert "- 정지(mask)" in result.stdout and "com.korellas.m" in result.stdout


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
