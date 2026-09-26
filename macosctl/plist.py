"""plist 생성 — 매니페스트 → LaunchDaemon plist 바이트.

설계 근거: docs/design.md

**출력은 install-services.sh와 바이트 단위로 같아야 한다.** 한 바이트라도 다르면
첫 `macosctl apply`가 전 서비스를 '변경'으로 판정해 가중치를 다시 읽는다 — D3가 없애려던
수 분 서빙 중단이 그대로 재발한다. 키 순서와 dict 구성이 구 인스톨러와 동일한 이유가
이것이고, `scripts/test_svc_apply.py`가 설치된 실물과 대조해 이를 고정한다.

비교는 정규화(`plutil -convert xml1`) 후에 한다 — 사람이 손으로 고친 plist나
포맷만 다른 경우를 '변경'으로 오판하지 않기 위해서다.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from macosctl.manifest import Defaults, Service


def _canonical(payload: dict) -> bytes:
    """설치본과 바이트가 같도록 직렬화한다.

    후행 개행을 떼는 이유: 구 install-services.sh는 plist를 bash 명령 치환
    (`body="$(base64 --decode ...)"`)으로 받아 `printf '%s'`로 썼고, 명령 치환은
    후행 개행을 잘라낸다. 그래서 설치된 실물에는 마지막 `\\n`이 없다.

    개행 하나 때문에 첫 apply가 전 서비스를 '변경'으로 판정하고 가중치를 다시
    읽는다 — 정규화 비교가 이를 흡수하긴 하지만, 바이트까지 맞춰두면 plutil이
    없거나 실패하는 경로에서도 안전하다. 비용이 0인 보험이다.
    """
    return plistlib.dumps(payload).rstrip(b"\n")


def build(svc: Service, defaults: Defaults) -> bytes:
    """서비스 하나의 plist 바이트. 키 구성은 install-services.sh와 동일하다."""
    service_user = getattr(svc, "user", None)
    if service_user is None:
        service_user = defaults.user
    log_dir = getattr(svc, "log_dir", None) or defaults.log_dir
    payload = {
        "Label": svc.label,
        "ProgramArguments": list(svc.exec_argv),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": defaults.throttle_seconds,
        "UserName": service_user,
        "WorkingDirectory": getattr(
            svc, "working_directory", defaults.working_directory
        ),
        "ProcessType": getattr(svc, "process_type", "Background"),
        "StandardOutPath": f"{log_dir}/{svc.name}.out.log",
        "StandardErrorPath": f"{log_dir}/{svc.name}.err.log",
        "EnvironmentVariables": {
            "PATH": defaults.path,
            "HOME": f"/Users/{service_user}",
            **dict(svc.env),
        },
    }
    return _canonical(payload)


def build_log_rotate(defaults: Defaults, repo: Path, config_file: Path) -> bytes:
    """로그 회전 잡.

    매니페스트의 [[service]]가 아니다 — 포트도 KeepAlive도 없는 주기 실행 잡이라
    그 스키마에 맞지 않는다. 하지만 **우리가 설치하는 정당한 잡**이므로 인벤토리와
    desired set에는 포함된다 (D4). 접두사만 보고 은퇴시키면 이걸 죽인다.
    """
    payload = {
        "Label": "com.korellas.log-rotate",
        "ProgramArguments": [f"{repo}/libexec/rotate-service-logs.sh"],
        "RunAtLoad": True,
        "StartInterval": defaults.log_rotate_interval_seconds,
        "UserName": defaults.user,
        "WorkingDirectory": str(repo),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "StandardOutPath": f"{defaults.log_dir}/log-rotate.out.log",
        "StandardErrorPath": f"{defaults.log_dir}/log-rotate.err.log",
        "EnvironmentVariables": {
            "PATH": defaults.path,
            "HOME": f"/Users/{defaults.user}",
            "MACOSCTL_CONFIG": str(config_file),
        },
    }
    return _canonical(payload)


def _normalized(data: bytes) -> bytes | None:
    """plutil로 정규화한다. 실패하면 None (비교 불가)."""
    try:
        result = subprocess.run(
            ["/usr/bin/plutil", "-convert", "xml1", "-o", "-", "-"],
            input=data, capture_output=True, timeout=10, check=False,
        )
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def same_as_installed(generated: bytes, target: Path) -> bool:
    """생성 결과가 설치본과 같은가. 정규화 후 비교한다 (2R M-4)."""
    try:
        installed = target.read_bytes()
    except OSError:
        return False
    if generated == installed:
        return True
    a, b = _normalized(generated), _normalized(installed)
    return a is not None and a == b


def lint(data: bytes) -> str | None:
    """plutil -lint. 통과면 None, 실패면 사유 문자열.

    launchd는 조용히 실패한다 — 깨진 plist를 bootstrap하면 원인을 찾기 어렵다.
    쓰기 전에 검사하는 것이 mklaunchd가 존재하는 이유이기도 하다.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/plutil", "-lint", "-"],
            input=data, capture_output=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return None
        return (result.stderr or result.stdout).decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"plutil 실행 실패: {exc}"
