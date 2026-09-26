"""서비스 명령 가드 훅 테스트."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "guard-service-commands.py"

loader = importlib.machinery.SourceFileLoader("hook_under_test", str(HOOK))
spec = importlib.util.spec_from_loader(loader.name, loader)
hook = importlib.util.module_from_spec(spec)
loader.exec_module(hook)


def _guides(command: str) -> bool:
    return hook.check(command) is not None


def test_catches_the_bypass_that_actually_happened():
    """환경변수 할당 뒤의 런처 실행도 감지한다."""
    assert _guides("MODEL=org/x SESSION=worker-b PORT=8002 ./run-mlx-lm.sh")


def test_catches_launcher_run_in_various_shapes():
    for command in (
        "./run-mtplx.sh",
        "/Users/example/project/run-mtplx.sh foreground",
        "cd ~/ai && ./run-litellm.sh start",
        "bash run-ds4.sh",
    ):
        assert _guides(command), command


def test_catches_launchctl_mutation_on_our_domain():
    for command in (
        "sudo launchctl bootout system/com.korellas.litellm",
        "launchctl kickstart -k system/com.korellas.worker-b",
        "sudo launchctl bootstrap system /Library/LaunchDaemons/com.korellas.x.plist",
        "sudo launchctl disable system/com.webtop",
    ):
        assert _guides(command), command


def test_read_only_launchctl_passes():
    for command in (
        "launchctl print system/com.korellas.litellm",
        "launchctl print-disabled system",
        "launchctl list | grep korellas",
    ):
        assert not _guides(command), command


def test_launcher_non_start_subcommands_pass():
    """기동하지 않는 호출은 안내 대상이 아니다.

    안전한 인자를 열거하는 방식으로 짰다가 `--help`나 오타까지 잡혔다. 서버를
    띄우는 것은 인자 없음/start/foreground 셋뿐이므로 그쪽을 열거한다.
    """
    for command in (
        "./run-mtplx.sh logs",
        "SESSION=worker-b ./run-mlx-lm.sh stop",
        "./run-litellm.sh attach",
        "./run-mlx-lm.sh --help",
        "./run-mlx-lm.sh bogus",
    ):
        assert not _guides(command), command


def test_svc_itself_passes():
    """svc가 내부적으로 하는 호출까지 잡으면 자기 발을 문다."""
    for command in (
        "svc restart worker-b",
        "sudo svc apply",
        "sudo /usr/local/sbin/macosctl-helper stop webtop",
        "sudo ./scripts/install-services.sh",
    ):
        assert not _guides(command), command


def test_unrelated_launchctl_passes():
    """다른 도메인은 우리 관심사가 아니다."""
    assert not _guides("sudo launchctl bootout system/com.apple.something")


def test_mentioning_a_launcher_is_not_running_it():
    """조회 명령은 런처 이름을 언급할 뿐이다.

    훅을 켠 첫 순간 실제로 여기 걸렸다 — 테스트 payload를 echo하는 명령이
    '런처 실행'으로 잡혔다. 오탐이 쌓이면 사람은 훅을 끄고, 그러면 진짜
    우회도 못 잡는다.
    """
    for command in (
        "grep -n run-mtplx.sh services.toml",
        "cat run-mlx-lm.sh",
        "git log --oneline -- run-litellm.sh",
        "rg 'run-ds4.sh' docs/",
    ):
        assert not _guides(command), command


def test_syntax_check_is_not_execution():
    """`bash -n`은 구문 검사다 — 실행이 아니다."""
    assert not _guides("bash -n run-mlx-lm.sh")
    assert not _guides("bash -n run-mlx-lm.sh && bash -n run-mtplx.sh")


def test_compound_command_is_judged_per_segment():
    """복합 명령을 통째로 보면 첫 매치 하나로 전체를 판정하게 된다."""
    # 검사 + 안전한 서브커맨드 — 어느 세그먼트도 기동이 아니다
    assert not _guides("bash -n run-mlx-lm.sh && ./run-mlx-lm.sh stop")
    # 뒤쪽 세그먼트가 진짜 기동이면 잡아야 한다
    assert _guides("echo hi && ./run-mtplx.sh foreground")


def test_unrelated_commands_pass():
    for command in ("ls -la", "git status", "python3 scripts/test_svc.py", ""):
        assert not _guides(command), command


def test_malformed_input_does_not_block():
    """훅이 입력을 못 읽었다고 도구 실행을 막으면 안 된다."""
    import io

    original = sys.stdin
    try:
        sys.stdin = io.StringIO("not json at all")
        assert hook.main() == 0
    finally:
        sys.stdin = original

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
