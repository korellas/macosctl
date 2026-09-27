"""apply 사전 검증 — launchd는 조용히 실패한다.

설계 근거: docs/design.md

깨진 정의로 bootstrap 하면 launchd는 아무 말 없이 잡을 안 띄우거나 무한 재시도에
빠진다. 원인을 찾는 데 걸리는 시간이 이 파일의 존재 이유다.

치명(fatal)과 경고(warning)를 구분한다 — 예산 초과처럼 판단이 필요한 것까지
중단시키면 사람이 검증을 우회하게 되고, 그러면 전체가 무의미해진다.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from macosctl import policy as policy_module
from macosctl.collect import parse_disabled_overrides
from macosctl.confd import PROCESS_TYPES
from macosctl.manifest import Defaults, Service

LABEL_PATTERN = re.compile(r"^com\.korellas\.[a-z0-9-]{1,40}$")
WEBTOP_LABEL = "com.webtop"

_UNIT = {"GB": 1, "G": 1, "TB": 1024, "T": 1024, "MB": 1 / 1024, "M": 1 / 1024}


@dataclass(frozen=True)
class Problem:
    code: str
    service: str
    detail: str
    fatal: bool = True


def _budget_gb(text: str | None) -> float:
    if not text:
        return 0.0
    match = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text)
    if not match:
        return 0.0
    return float(match.group(1)) * _UNIT.get(match.group(2).upper(), 0)


def _physical_memory_gb() -> float | None:
    """물리 메모리를 감지한다. 감지 실패는 예산 경고만 비활성화한다."""
    try:
        result = subprocess.run(
            ("sysctl", "-n", "hw.memsize"),
            check=True,
            capture_output=True,
            text=True,
        )
        return int(result.stdout.strip()) / (1024 ** 3)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _boot_disabled_labels() -> frozenset[str]:
    """boot-disabled 라벨 집합. 조회 실패는 필터링 없이(=전량 합산) 폴백한다 —

    과소경고보다 과대경고가 안전하다.
    """
    try:
        result = subprocess.run(
            ("/bin/launchctl", "print-disabled", "system"),
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return frozenset()
    overrides = parse_disabled_overrides(result.stdout)
    return frozenset(label for label, disabled in overrides.items() if disabled)


def check(
    services: tuple[Service, ...],
    defaults: Defaults,
    *,
    label_policy: policy_module.Policy | None = None,
) -> tuple[Problem, ...]:
    problems: list[Problem] = []

    if defaults.user == "root":
        # UserName은 매니페스트 유래다. user=root + 악성 exec 조합이면 운영자가
        # 루틴으로 치는 apply 한 번이 root 프로세스를 만든다 (2R Codex #M-2).
        problems.append(
            Problem("root-user", "[defaults]",
                    "user가 root다 — 데몬을 root로 띄우면 권한 상승 경로가 된다")
        )

    known = {s.name for s in services}
    seen_labels: dict[str, str] = {}
    seen_ports: dict[int, str] = {}

    for svc in services:
        if getattr(svc, "user", None) == "root":
            problems.append(
                Problem(
                    "root-user",
                    svc.name,
                    "user가 root다 — 데몬을 root로 띄우면 권한 상승 경로가 된다",
                )
            )

        if label_policy is None:
            bad_label = not (
                LABEL_PATTERN.match(svc.label)
                or (svc.label == WEBTOP_LABEL and svc.name == "webtop")
            )
        else:
            bad_label = policy_module.resolve_label(svc.name, label_policy) != svc.label
        if bad_label:
            # label이 자유 필드면 타사 plist를 덮어쓰거나 macosctl-helper의 라벨 조립 규칙과
            # 어긋나 제어 불가가 된다 (2R Codex #M-1).
            if label_policy is None:
                detail = f"label '{svc.label}'이 com.korellas.<name> 형식이 아니다"
            else:
                detail = f"label '{svc.label}'이 policy의 name 매핑과 다르다"
            problems.append(Problem("bad-label", svc.name, detail))

        process_type = getattr(svc, "process_type", "Background")
        if process_type not in PROCESS_TYPES:
            problems.append(
                Problem("bad-process-type", svc.name,
                        f"process_type '{process_type}'이 {PROCESS_TYPES} 중 하나가 아니다")
            )

        if svc.label in seen_labels:
            problems.append(
                Problem("duplicate-label", svc.name,
                        f"label '{svc.label}'이 {seen_labels[svc.label]}와 중복이다")
            )
        seen_labels[svc.label] = svc.name

        if svc.port in seen_ports:
            problems.append(
                Problem("duplicate-port", svc.name,
                        f"포트 {svc.port}가 {seen_ports[svc.port]}와 중복이다")
            )
        seen_ports[svc.port] = svc.name

        for dep in svc.depends_on:
            if dep not in known:
                problems.append(
                    Problem("dangling-dependency", svc.name,
                            f"depends_on이 존재하지 않는 '{dep}'를 가리킨다")
                )

        if svc.exec_argv:
            binary = svc.exec_argv[0]
            if not os.path.isfile(binary):
                problems.append(
                    Problem("exec-missing", svc.name, f"실행 파일이 없다: {binary}")
                )
            elif not os.access(binary, os.X_OK):
                problems.append(
                    Problem("exec-not-executable", svc.name,
                            f"실행 권한이 없다: {binary}")
                )

        env = dict(svc.env)
        if "PORT" in env and str(svc.port) != str(env["PORT"]):
            # port와 env.PORT는 이중 선언이다. 한쪽만 고치면 doctor가 틀린 포트를
            # 찔러 오탐한다 (2R Fable M-2).
            problems.append(
                Problem("inconsistent-port", svc.name,
                        f"port={svc.port}인데 env.PORT={env['PORT']}다")
            )
        if "MEMORY_BUDGET" in env and svc.mem_budget:
            if _budget_gb(env["MEMORY_BUDGET"]) != _budget_gb(svc.mem_budget):
                problems.append(
                    Problem("inconsistent-budget", svc.name,
                            f"mem_budget={svc.mem_budget}인데 "
                            f"env.MEMORY_BUDGET={env['MEMORY_BUDGET']}다")
                )

    boot_disabled = _boot_disabled_labels()
    total = sum(
        _budget_gb(s.mem_budget)
        for s in services
        if s.managed and s.label not in boot_disabled
    )
    physical_memory_gb = _physical_memory_gb()
    if physical_memory_gb is not None and total > physical_memory_gb:
        problems.append(
            Problem("memory-overcommit", "[전체]",
                    f"managed+boot-enabled 선언 예산 합계 {total:.0f}GB > 물리 "
                    f"{physical_memory_gb:g}GB — 동시 로딩 시 메모리가 부족해질 수 있다",
                    fatal=False)
        )

    return tuple(problems)


def has_fatal(problems: tuple[Problem, ...]) -> bool:
    return any(p.fatal for p in problems)
