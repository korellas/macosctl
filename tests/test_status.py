"""Production ``macosctl status`` rendering and JSON contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from macosctl import collect, model, status  # noqa: E402


def _service(
    name: str,
    port: int,
    *,
    managed: bool = True,
    lifecycle: str = "active",
) -> model.MergedService:
    return model.MergedService(
        name=name,
        label=f"com.korellas.{name}",
        port=port,
        group="demo",
        managed=managed,
        exec_argv=("/usr/bin/true",),
        depends_on=(),
        mem_budget=None,
        env=(),
        working_directory="/tmp",
        lifecycle=lifecycle,
        sources=(model.Provenance("name", "30-demo.toml"),),
    )


def _fixture():
    services = (
        _service("litellm", 4000),
        _service("mlx-large", 8011),
        _service("ollama", 11434),
        _service("postgres", 5432),
        _service("redis", 6379, lifecycle="masked"),
        _service("dev-proxy", 8080, managed=False),
    )
    observed = collect.SystemState(
        installed_labels=frozenset({
            "com.korellas.litellm",
            "com.korellas.mlx-large",
            "com.korellas.ollama",
        }),
        jobs={
            "com.korellas.litellm": collect.Job(
                "com.korellas.litellm", True, "running", 100
            ),
            "com.korellas.mlx-large": collect.Job(
                "com.korellas.mlx-large", True, "running", 200
            ),
        },
        listeners=(
            collect.Listener(4000, 101, "python"),
            collect.Listener(11434, 301, "ollama"),
            collect.Listener(8080, 401, "node"),
        ),
        ppids={101: 100, 301: 1, 401: 1},
        disabled_overrides={
            "com.korellas.litellm": False,
            "com.korellas.redis": True,
        },
    )
    return services, observed


def test_build_rows_classifies_every_human_status():
    services, observed = _fixture()

    rows = status.build_rows(services, observed)

    assert {row.name: row.state for row in rows} == {
        "litellm": "running",
        "mlx-large": "down",
        "ollama": "rogue",
        "postgres": "missing",
        "redis": "masked",
        "dev-proxy": "external",
    }


def test_running_row_prefers_the_owned_listener_pid_over_a_foreign_listener():
    service = _service("demo", 7777)
    observed = collect.SystemState(
        installed_labels=frozenset({service.label}),
        jobs={service.label: collect.Job(service.label, True, "running", 100)},
        listeners=(
            collect.Listener(7777, 900, "foreign"),
            collect.Listener(7777, 101, "owned"),
        ),
        ppids={900: 1, 101: 100},
        disabled_overrides={},
    )

    row = status.build_rows((service,), observed)[0]

    assert row.state == "running"
    assert row.pid == 101


def test_render_text_uses_approved_badges_and_right_aligned_numbers():
    services, observed = _fixture()
    output = status.render_text(status.build_rows(services, observed), width=100, color=False)

    for badge in (
        "● RUNNING", "○ DOWN", "▲ ROGUE", "× MISSING", "◎ MASKED", "◇ EXTERNAL"
    ):
        assert badge in output
    assert "STATE" in output and "SERVICE" in output and "PORT" in output
    assert ":4000" not in output
    assert "\x1b[" not in output

    lines = output.splitlines()
    service_rows = [line for line in lines if any(s.name in line for s in services)]
    port_ends = {
        line.index(str(service.port)) + len(str(service.port))
        for line, service in zip(service_rows, services, strict=True)
    }
    assert len(port_ends) == 1


def test_render_json_is_stable_machine_data_without_terminal_formatting():
    services, observed = _fixture()
    payload = json.loads(status.render_json(status.build_rows(services, observed)))

    assert payload["schema"] == 1
    assert payload["summary"] == {
        "total": 6,
        "running": 1,
        "issues": 3,
        "intentional": 2,
        "overrides": 1,
    }
    by_name = {row["name"]: row for row in payload["services"]}
    assert by_name["litellm"]["listeners"] == [
        {"command": "python", "owned": True, "pid": 101}
    ]
    assert by_name["redis"]["disabled_override"] is True
    assert "\x1b[" not in status.render_json(status.build_rows(services, observed))


def test_macosctl_entrypoint_identifies_itself():
    result = subprocess.run(
        [sys.executable, str(REPO / "bin" / "macosctl"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: macosctl ")


def test_macosctl_rejects_too_narrow_status_width():
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "macosctl"),
            "status",
            "--width",
            "39",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "40" in result.stderr
    assert "Traceback" not in result.stderr



def test_human_summary_names_boot_disabled_instead_of_overrides():
    """사람이 읽는 줄에서는 'overrides'가 아니라 무엇이 막혔는지를 말한다.

    이제 boot policy와 runtime이 분리됐으므로, 그 칸이 세는 것은 "오버라이드가
    있다"가 아니라 "부팅 때 뜨지 않는다"다.
    """
    services, observed = _fixture()
    text = status.render_text(
        status.build_rows(services, observed), width=120, color=False
    )
    assert "boot-disabled" in text, text
    assert "overrides" not in text, text


def test_json_summary_keeps_the_schema_1_overrides_key():
    """Webtop이 소비하는 키는 그대로다 — 사람용 명칭 변경이 스키마를 흔들면 안 된다."""
    services, observed = _fixture()
    payload = json.loads(
        status.render_json(status.build_rows(services, observed))
    )
    assert payload["schema"] == 1
    assert "overrides" in payload["summary"]


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
    raise SystemExit(1 if failures else 0)
