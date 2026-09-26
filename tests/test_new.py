"""svc new 스캐폴더 단위 테스트."""

import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import manifest  # noqa: E402
from macosctl import new  # noqa: E402
from macosctl import policy  # noqa: E402
from macosctl import validate  # noqa: E402
from _manifest_source import MANIFEST  # noqa: E402


def _parse_block(block: str) -> dict:
    """생성된 블록만 단독으로 파싱한다."""
    return tomllib.loads(block)["service"][0]


def test_generated_block_is_valid_toml():
    block = new.render_block("qwen-omni", port=8004, group="mlx",
                             model="mlx-community/Qwen3.5-Omni-30B")
    entry = _parse_block(block)
    assert entry["name"] == "qwen-omni"
    assert entry["label"] == "com.korellas.qwen-omni"
    assert entry["port"] == 8004
    assert entry["group"] == "mlx"


def test_generated_block_uses_the_policy_derived_label_when_supplied():
    block = new.render_block(
        "demo",
        port=8004,
        group="infra",
        label="org.example.demo",
    )
    assert _parse_block(block)["label"] == "org.example.demo"


def test_new_checks_name_and_group_against_the_loaded_policy():
    loaded_policy = policy.Policy(
        label_prefix="org.example.",
        label_exceptions={},
        service_user="example",
        groups=("infra",),
    )

    assert new.check_conflicts(
        "demo", 9999, (), group="infra", label_policy=loaded_policy
    ) == ()
    assert any(
        "그룹" in problem
        for problem in new.check_conflicts(
            "demo", 9999, (), group="edge", label_policy=loaded_policy
        )
    )
    assert new.check_conflicts(
        "---", 9999, (), group="infra", label_policy=loaded_policy
    )


def test_generated_block_passes_validation():
    """생성물이 apply의 사전 검증(D5)을 그대로 통과해야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        launcher_dir = Path(tmp)
        launcher = launcher_dir / "run-mlx-lm.sh"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        block = new.render_block("qwen-omni", port=8004, group="mlx",
                                 model="example/model", launcher_dir=launcher_dir)
        entry = _parse_block(block)
        svc = manifest.Service(
            name=entry["name"], label=entry["label"], port=entry["port"],
            group=entry["group"], managed=entry.get("managed", True),
            exec_argv=tuple(entry["exec"]), depends_on=tuple(entry.get("depends_on", ())),
            mem_budget=entry.get("mem_budget"),
            env=tuple(sorted(entry.get("env", {}).items())),
        )
        problems = validate.check((svc,), manifest.load_defaults(MANIFEST))
    fatal = [p for p in problems if p.fatal]
    assert not fatal, f"생성된 블록이 검증에 걸린다: {fatal}"


def test_mlx_group_reuses_shared_launcher():
    """mlx 서비스는 run-mlx-lm.sh 하나를 env로 구분해 공유한다."""
    entry = _parse_block(new.render_block("x-mlx", port=8010, group="mlx",
                                          model="org/model"))
    assert entry["exec"][0].endswith("run-mlx-lm.sh")
    assert entry["env"]["MODEL"] == "org/model"
    assert entry["env"]["PORT"] == "8010"
    assert entry["env"]["SESSION"] == "x-mlx"


def test_port_and_env_port_agree():
    """이중 선언 불일치는 D5가 잡는 항목이다 — 생성 단계에서 만들지 않는다."""
    entry = _parse_block(new.render_block("x-mlx", port=8010, group="mlx",
                                          model="org/model"))
    assert str(entry["port"]) == entry["env"]["PORT"]


def test_non_mlx_group_gets_own_launcher_path():
    entry = _parse_block(new.render_block("thing", port=8020, group="edge"))
    assert entry["exec"][0].endswith("run-thing.sh")
    assert entry["exec"][1] == "foreground"


def test_rejects_existing_name():
    services = manifest.load(MANIFEST)
    problems = new.check_conflicts(services[0].name, 9999, services)
    assert any("이름" in p for p in problems), problems


def test_rejects_used_port():
    services = manifest.load(MANIFEST)
    problems = new.check_conflicts("brand-new", services[0].port, services)
    assert any("포트" in p for p in problems), problems


def test_rejects_bad_name_shape():
    """svcctl의 이름 규칙과 같아야 한다 — 통과 못 할 이름을 만들어주면 안 된다."""
    services = manifest.load(MANIFEST)
    for bad in ("Upper", "with space", "dot.name", "x" * 41, ""):
        assert new.check_conflicts(bad, 9999, services), f"허용하면 안 됨: {bad!r}"


def test_accepts_fresh_name_and_port():
    services = manifest.load(MANIFEST)
    assert new.check_conflicts("brand-new", 9999, services) == ()


def test_append_keeps_manifest_parseable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "services.toml"
        path.write_text(MANIFEST.read_text())
        before = len(manifest.load(path))

        block = new.render_block("brand-new", port=9999, group="edge")
        new.append_block(path, block)

        after = manifest.load(path)
        assert len(after) == before + 1
        assert any(s.name == "brand-new" for s in after)


def test_append_does_not_disturb_existing_entries():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "services.toml"
        path.write_text(MANIFEST.read_text())
        before = {s.name: s for s in manifest.load(path)}

        new.append_block(path, new.render_block("brand-new", port=9999, group="edge"))

        after = {s.name: s for s in manifest.load(path)}
        for name, svc in before.items():
            assert after[name] == svc, f"{name}이 바뀌었다"


def test_cli_new_is_draft_only_and_has_no_legacy_default_manifest_write():
    """D13 gives the destination to the project owner, then `svc link` attaches it."""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "macosctl"),
            "new",
            "brand-new",
            "--port",
            "9999",
            "--group",
            "edge",
            "--write",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --write" in result.stderr
    assert "DEFAULT_MANIFEST" not in (REPO / "bin" / "macosctl").read_text()


def test_cli_new_reads_policy_and_reports_security_refusal_cleanly():
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "conf.d").mkdir()
        (root / "macosctl.toml").write_text(
            'schema = 1\n[defaults]\nworking_directory = "/srv"\n'
        )
        (root / "policy.json").write_text(
            '{"schema":1,"label_prefix":"com.korellas.",'
            '"label_exceptions":{},"service_user":"example",'
            '"groups":["edge"]}'
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "macosctl"),
                "--config-root",
                str(root),
                "new",
                "brand-new",
                "--port",
                "9999",
                "--group",
                "edge",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "설정 디렉터리" in output
    assert "Traceback" not in output

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
