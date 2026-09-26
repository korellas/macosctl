#!/usr/bin/env python3
"""PreToolUse 훅 — 서비스를 macosctl 밖에서 만지려 할 때 안내한다.

설계 근거: docs/design.md

**차단하지 않고 안내한다.** 정당한 디버깅까지 막으면 사람이 훅을 꺼버리고, 그게
최악의 결과다. 목표는 우회를 불가능하게 만드는 것이 아니라 정도(正道)를 알려주는
것이며, 실제 우회 감지는 `macosctl doctor`가 사후에 맡는다.

겨냥하는 것:
  · launchctl로 우리 도메인을 직접 조작 (감사 기록이 안 남고 인가를 우회한다)
  · run-*.sh를 상시 기동 용도로 실행

겨냥하지 않는 것:
  · launchctl print / print-disabled — 읽기는 얼마든지
  · run-*.sh stop|logs|attach — 운영에 필요한 보조 동작
  · macosctl 자신이 내부적으로 하는 호출


"""

import json
import re
import sys

# 우리 도메인을 바꾸는 launchctl 동사만. print 류는 읽기이므로 건드리지 않는다.
LAUNCHCTL_MUTATION = re.compile(
    r"\blaunchctl\s+(bootstrap|bootout|kickstart|enable|disable)\b[^|;&]*"
    r"(system/com\.korellas|system/com\.webtop|/Library/LaunchDaemons)"
)

# run-*.sh 실행.
#
# **기동하는 인자만** 겨냥한다. 안전한 인자를 열거하는 방식(stop|logs|attach만
# 통과)으로 짰다가, 목록에 없는 것은 전부 잡히는 문제가 있었다 — `--help`나
# 오타처럼 서비스를 띄우지 않는 호출까지 안내를 받는다.
#
# 위험한 쪽을 열거하는 것이 정확하다: 인자 없음, start, foreground 셋만이
# 서버를 띄운다.
LAUNCHER_RUN = re.compile(r"(?:^|[\s;&|])(?:\./)?(?:\S*/)?run-([a-z0-9-]+)\.sh\b\s*([\w-]+)?")
LAUNCHER_START_ARGS = {"", "start", "foreground"}


# 첫 토큰이 이것들이면 통과. 런처 이름을 **언급**할 뿐 실행하지 않는다.
#
# 이 목록이 없으면 `grep run-mtplx.sh`나 `echo '...run-mlx-lm.sh...'` 같은 조회가
# 전부 안내를 받는다.
# 오탐이 쌓이면 사람은 훅을 끄고, 그러면 진짜 우회도 못 잡는다.
INSPECTORS = frozenset({
    "echo", "cat", "grep", "rg", "ls", "head", "tail", "wc", "find",
    "git", "diff", "less", "printf", "awk", "sed", "python3", "python",
})


def _segments(command: str) -> list[str]:
    """복합 명령을 실행 단위로 쪼갠다.

    통째로 보면 `bash -n run-x.sh && ./run-x.sh stop` 같은 줄에서 첫 매치 하나로
    전체를 판정하게 된다. 실제로 구문 검사(`bash -n`)와 안전한 서브커맨드가
    섞인 명령도 구분해야 한다.
    """
    return [s for s in re.split(r"&&|\|\||[;|\n]", command) if s.strip()]


def _is_inspection(segment: str) -> bool:
    """이 세그먼트가 실행이 아니라 조회/검사인가."""
    tokens = segment.strip().split()
    # 앞의 환경변수 할당은 건너뛴다 (MODEL=x ./run-...)
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return True

    head = tokens[0].split("/")[-1]
    if head in INSPECTORS:
        return True
    # `bash -n foo.sh` 는 구문 검사지 실행이 아니다.
    if head in {"bash", "sh", "zsh"} and "-n" in tokens[1:2]:
        return True
    return False


def check(command: str) -> str | None:
    """안내가 필요하면 메시지를, 아니면 None."""
    for segment in _segments(command):
        if not _is_inspection(segment):
            message = _check_segment(segment)
            if message:
                return message
    return None


def _check_segment(command: str) -> str | None:
    if re.search(r"\b(?:macosctl|macosctl-helper|svc|svcctl)\b|install-services\.sh|install-svcctl\.sh", command):
        return None  # 관리 도구 자신의 경로는 통과

    if LAUNCHCTL_MUTATION.search(command):
        return (
            "launchctl로 이 스택의 서비스를 직접 조작하려 한다.\n"
            "  svc를 쓰면 인가와 감사 기록이 함께 간다:\n"
            "    macosctl start|stop|restart|enable|disable <name>\n"
            "    sudo macosctl apply        # 등록·은퇴\n"
            "  절차: ~/git/macosctl/README.md"
        )

    match = LAUNCHER_RUN.search(command)
    if match:
        name, arg = match.group(1), (match.group(2) or "").strip()
        if arg not in LAUNCHER_START_ARGS:
            return None
        return (
            f"run-{name}.sh를 직접 실행하려 한다.\n"
            f"  일회성 실험이면 그대로 진행해도 된다. 다만 상시 서비스로 둘 거라면\n"
            f"  launchd 밖이라 재부팅에 뜨지 않고 macosctl doctor가 rogue로 잡는다.\n"
            f"    macosctl restart {name}    # 이미 등록된 서비스라면\n"
            f"    macosctl new {name} ...    # 새로 올릴 거라면\n"
            f"  절차: ~/git/macosctl/README.md"
        )

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 훅이 입력을 못 읽었다고 도구 실행을 막지는 않는다

    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command:
        return 0

    message = check(command)
    if not message:
        return 0

    # exit 2 = 안내를 모델에게 전달하고 도구 호출은 되돌린다.
    # (deny가 아니라 재고 요청 — 사람이 정말 필요하면 그대로 다시 실행하면 된다.)
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
