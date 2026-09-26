"""시스템 실측 수집 — 파서와 라이브 수집기.

설계 근거: docs/design.md

파서를 라이브 수집과 분리한 이유: doctor를 픽스처로 회귀 테스트하기 위해서다.
따라서 파서는 raw 명령 출력을 그대로 먹어야 하며, 라이브 경로도 같은 파서를 쓴다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Listener:
    port: int
    pid: int
    command: str


@dataclass(frozen=True)
class Job:
    label: str
    loaded: bool
    state: str | None = None
    pid: int | None = None


@dataclass(frozen=True)
class SystemState:
    installed_labels: frozenset[str]
    jobs: dict[str, Job]
    listeners: tuple[Listener, ...]
    ppids: dict[int, int]
    disabled_overrides: dict[str, bool]
    # macosctl의 **의도** 두 축. 실제 사실(job/PID/포트/오버라이드)과 섞지 않는다.
    stopped_this_boot: frozenset[str] = frozenset()
    disabled_by_macosctl: frozenset[str] = frozenset()

    def listeners_on(self, port: int) -> tuple[Listener, ...]:
        return tuple(l for l in self.listeners if l.port == port)

    def is_descendant(self, pid: int, ancestor: int) -> bool:
        """pid가 ancestor의 자손(또는 자신)인가.

        PID 동일성으로 소유권을 판정하면 안 된다 — run-mtplx.sh는 부트 락을 쥐기
        위해 exec을 쓰지 않고 부모 래퍼를 남기므로, launchd job PID와 포트를 쥔
        PID가 정상적으로 다르다.
        """
        seen = set()
        cur = pid
        while cur and cur not in seen:
            if cur == ancestor:
                return True
            seen.add(cur)
            cur = self.ppids.get(cur, 0)
        return False


# --- 파서 -----------------------------------------------------------------------

_LSOF_NAME = re.compile(r":(\d+)\s+\(LISTEN\)")


def parse_listeners(text: str) -> tuple[Listener, ...]:
    """`lsof -nP -iTCP -sTCP:LISTEN` 출력을 읽는다.

    IPv4/IPv6로 같은 프로세스가 두 줄 잡히는 경우를 (포트, PID)로 중복 제거한다 —
    안 하면 정상 postgres가 '중복 인스턴스'로 오탐된다.
    """
    found: dict[tuple[int, int], Listener] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("COMMAND"):
            continue
        m = _LSOF_NAME.search(line)
        if not m:
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        port, pid = int(m.group(1)), int(parts[1])
        found.setdefault((port, pid), Listener(port=port, pid=pid, command=parts[0]))
    return tuple(found.values())


def parse_jobs(text: str) -> dict[str, Job]:
    """`launchctl print system/<label>` 출력을 읽는다.

    픽스처는 `## <label>` 헤더로 여러 블록을 이어붙인 형태다. 블록 안에서 첫
    `state =` 만 job의 상태다 — 그 뒤 더 깊이 들여쓰인 `state = active`는
    endpoint의 것이라 이걸 job 상태로 읽으면 전부 active로 오독한다.
    """
    jobs: dict[str, Job] = {}
    label: str | None = None
    state: str | None = None
    pid: int | None = None
    loaded = False

    def flush() -> None:
        if label:
            jobs[label] = Job(label=label, loaded=loaded, state=state, pid=pid)

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            label, state, pid, loaded = line[3:].strip(), None, None, False
            continue
        stripped = line.strip()
        if stripped == "(not loaded)":
            loaded = False
        elif stripped.startswith("state = ") and state is None:
            state, loaded = stripped[len("state = "):].strip(), True
        elif stripped.startswith("pid = ") and pid is None:
            value = stripped[len("pid = "):].strip()
            if value.isdigit():
                pid, loaded = int(value), True
    flush()
    return jobs


def parse_installed_labels(text: str) -> frozenset[str]:
    """`ls -la /Library/LaunchDaemons` 출력에서 설치된 label을 뽑는다."""
    labels = set()
    for line in text.splitlines():
        for token in line.split():
            if token.endswith(".plist"):
                labels.add(token[: -len(".plist")])
    return frozenset(labels)


def parse_ppids(text: str) -> dict[int, int]:
    """`ps -Ao pid,ppid,...` 출력에서 pid→ppid 매핑을 만든다."""
    ppids: dict[int, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            ppids[int(parts[0])] = int(parts[1])
    return ppids


_OVERRIDE = re.compile(r'"([^"]+)"\s*=>\s*(\w+)')


def parse_disabled_overrides(text: str) -> dict[str, bool]:
    """`launchctl print-disabled system` 출력 → {label: disabled 여부}."""
    return {
        m.group(1): m.group(2).lower() == "disabled"
        for m in (_OVERRIDE.search(line) for line in text.splitlines())
        if m
    }


# --- 라이브 수집 ----------------------------------------------------------------

DAEMON_DIR = Path("/Library/LaunchDaemons")


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def collect_live(labels: tuple[str, ...], ports: tuple[int, ...]) -> SystemState:
    """라이브 시스템에서 doctor 입력을 모은다. 파서는 픽스처 경로와 공유한다."""
    installed = frozenset(
        p.name[: -len(".plist")] for p in DAEMON_DIR.glob("*.plist")
    )

    job_blocks = []
    for label in labels:
        job_blocks.append(f"## {label}")
        out = _run(["/bin/launchctl", "print", f"system/{label}"])
        job_blocks.append(out if out.strip() else "(not loaded)")

    lsof_argv = ["/usr/sbin/lsof", "-nP", "-sTCP:LISTEN"]
    for port in ports:
        lsof_argv += ["-iTCP:%d" % port]

    return SystemState(
        installed_labels=installed,
        jobs=parse_jobs("\n".join(job_blocks)),
        listeners=parse_listeners(_run(lsof_argv)),
        ppids=parse_ppids(_run(["/bin/ps", "-Ao", "pid,ppid,command"])),
        disabled_overrides=parse_disabled_overrides(
            _run(["/bin/launchctl", "print-disabled", "system"])
        ),
        **_operator_intent(),
    )


def _operator_intent() -> dict[str, frozenset[str]]:
    """macosctl이 남긴 의도를 읽는다 — 실제 사실이 아니라 의도다.

    정지 의도는 **현재 boot session에 귀속된 것만** 스냅샷에 넣는다. 다른 부팅에서
    남은 마크를 그대로 들이면 doctor가 이미 만료된 의도를 근거로 "운영자가
    정지시켰다"고 말한다 — 재부팅이 RunAtLoad로 되살린 서비스에 대고.

    state 파일은 0644라 일반 유저도 읽는다. 읽지 못하거나 손상됐으면 빈 것으로
    둔다 — 관측 경로가 의도 파일 때문에 통째로 죽으면 안 된다. 손상 자체는
    macosctl-helper과 apply가 각자 자기 자리에서 fail-closed로 다룬다.
    """
    from macosctl import state

    try:
        boot_uuid = state.boot_session_uuid()
        desired = state.read(boot_uuid=boot_uuid)
    except Exception:  # noqa: BLE001 — 관측은 어떤 이유로도 멎지 않는다
        return {
            "stopped_this_boot": frozenset(),
            "disabled_by_macosctl": frozenset(),
        }
    return {
        "stopped_this_boot": desired.stopped_this_boot(boot_uuid),
        "disabled_by_macosctl": desired.disabled_by_macosctl,
    }


def collect_fixture(directory: Path) -> SystemState:
    """캡처된 드리프트 픽스처를 doctor 입력으로 되살린다."""

    def read(name: str) -> str:
        path = directory / name
        return path.read_text() if path.exists() else ""

    return SystemState(
        installed_labels=parse_installed_labels(read("installed-plists.txt")),
        jobs=parse_jobs(read("launchctl-print.txt")),
        listeners=parse_listeners(read("lsof-listen.txt")),
        ppids=parse_ppids(read("processes.txt")),
        disabled_overrides=parse_disabled_overrides(read("print-disabled.txt")),
    )
