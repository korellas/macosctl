"""services.toml 파싱 — svc와 install-services.sh가 공유하는 단일 파서.

설계 근거: docs/design.md

두 가지 뷰를 제공하는 것이 요점이다:
  · load()         — 무필터. managed=false 항목도 포함한다.
  · managed_only() — launchd에 등록할 대상만.

install-services.sh의 인라인 파서는 managed 항목만 뽑아 쓴다. 그건 등록기 입장에선
맞지만, status/doctor는 "정의는 있는데 꺼져 있는" 서비스도 알아야 한다 — 수동 기동하는 것을 '모르는 서비스'로 취급하면 rogue 오탐이 난다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Defaults:
    # conf.d mode resolves this solely from root-owned policy (D2-5).  The
    # legacy single-file reader still supplies a string until Task 4 wires the
    # policy reader into the new CLI path.
    user: str | None
    working_directory: str
    log_dir: str
    throttle_seconds: int
    path: str
    log_rotate_interval_seconds: int
    log_max_mb: int = 20
    log_keep: int = 5


def load_defaults(path: Path | str) -> Defaults:
    raw = tomllib.loads(Path(path).read_text()).get("defaults", {})
    return Defaults(
        user=raw.get("user", "root"),
        working_directory=raw.get("working_directory", "/"),
        log_dir=raw.get("log_dir", "/tmp"),
        throttle_seconds=int(raw.get("throttle_seconds", 10)),
        path=raw.get("path", "/usr/bin:/bin:/usr/sbin:/sbin"),
        log_rotate_interval_seconds=int(raw.get("log_rotate_interval_seconds", 900)),
        log_max_mb=int(raw.get("log_max_mb", 20)),
        log_keep=int(raw.get("log_keep", 5)),
    )


@dataclass(frozen=True)
class Service:
    name: str
    label: str
    port: int
    group: str
    managed: bool
    exec_argv: tuple[str, ...]
    depends_on: tuple[str, ...]
    mem_budget: str | None
    env: tuple[tuple[str, str], ...]


def load(path: Path | str) -> tuple[Service, ...]:
    """매니페스트 전체를 읽는다 (managed 여부와 무관)."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"매니페스트를 찾을 수 없다: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"매니페스트 파싱 실패 ({path}): {exc}")

    services = []
    for entry in raw.get("service", []):
        missing = [k for k in ("name", "label", "port", "exec") if k not in entry]
        if missing:
            raise SystemExit(
                f"매니페스트 항목에 필수 필드가 없다: {missing} — {entry.get('name', entry)}"
            )
        services.append(
            Service(
                name=entry["name"],
                label=entry["label"],
                port=int(entry["port"]),
                group=entry.get("group", "?"),
                managed=bool(entry.get("managed", True)),
                exec_argv=tuple(entry["exec"]),
                depends_on=tuple(entry.get("depends_on", ())),
                mem_budget=entry.get("mem_budget"),
                env=tuple(sorted(entry.get("env", {}).items())),
            )
        )
    return tuple(services)


def managed_only(services: tuple[Service, ...]) -> tuple[Service, ...]:
    return tuple(s for s in services if s.managed)


def dangling_dependencies(services: tuple[Service, ...]) -> tuple[tuple[str, str], ...]:
    """존재하지 않는 서비스를 가리키는 depends_on을 (서비스, 참조) 쌍으로 돌려준다.

    """
    known = {s.name for s in services}
    return tuple(
        (s.name, dep)
        for s in services
        for dep in s.depends_on
        if dep not in known
    )
