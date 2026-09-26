"""``macosctl _complete-names`` — confd-only service name listing for shell completion."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


DEFAULTS = '''\
schema = 1
[defaults]
working_directory = "/srv/default"
log_dir = "/tmp/logs"
throttle_seconds = 10
path = "/usr/bin:/bin"
log_rotate_interval_seconds = 900
log_max_mb = 20
log_keep = 5
'''


def _fragment(name: str, port: int) -> str:
    return f'''\
schema = 1
[[service]]
name = "{name}"
label = "com.korellas.{name}"
port = {port}
group = "infra"
exec = ["/bin/echo", "{name}"]
depends_on = []
'''


def _root(tmp: str) -> Path:
    root = Path(tmp)
    (root / "conf.d").mkdir()
    (root / "macosctl.toml").write_text(DEFAULTS)
    return root


def _run(config_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "macosctl"),
         "--config-root", str(config_root), "_complete-names"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_lists_declared_names_sorted_one_per_line():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        (root / "conf.d" / "30-a.toml").write_text(_fragment("zeta", 4001))
        (root / "conf.d" / "30-b.toml").write_text(_fragment("alpha", 4000))

        result = _run(root)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "alpha\nzeta\n"


def test_empty_config_yields_empty_output_not_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)

        result = _run(root)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


def _top_level_help() -> str:
    result = subprocess.run(
        [sys.executable, str(REPO / "bin" / "macosctl"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _canonical_command_help() -> dict[str, str]:
    """`macosctl --help`의 서브커맨드 한 줄 요약 = 의미론의 정본."""
    lines = _top_level_help().splitlines()
    start = lines.index("positional arguments:") + 1
    canonical = {}
    for line in lines[start:]:
        if line and not line.startswith(" "):  # "options:" 등 다음 절
            break
        m = re.match(r"^ {4}([A-Za-z_][\w-]*) {2,}(\S.*)$", line)
        if m:
            canonical[m.group(1)] = m.group(2).strip()
    return canonical


def _completion_command_help() -> dict[str, str]:
    """completions/_macosctl의 정적 'name:설명' 목록."""
    text = (REPO / "completions" / "_macosctl").read_text()
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\s*'([A-Za-z_][\w-]*):(.+)'$", text, re.MULTILINE)
    }


def test_completion_descriptions_match_canonical_cli_help():
    canonical = _canonical_command_help()
    completion = _completion_command_help()

    assert completion, "completion에서 명령 목록을 못 읽었다"
    mismatched = {
        name: (desc, canonical.get(name))
        for name, desc in completion.items()
        if canonical.get(name) != desc
    }
    assert not mismatched, f"CLI help와 어긋난 completion 설명: {mismatched}"


def test_complete_names_is_listed_in_help_and_marked_internal():
    """숨기지 않는다 — help에 '내부용'으로 표시된 채 노출하는 것이 현재 정책이다."""
    stdout = _top_level_help()

    assert "_complete-names" in stdout
    assert "==SUPPRESS==" not in stdout
    help_line = next(
        line for line in stdout.splitlines() if "_complete-names" in line and "  " in line.strip()
    )
    assert "내부용" in help_line, help_line


def test_completion_calls_complete_names_internal_not_hidden():
    """completion 주석도 같은 정책을 말해야 한다 — hidden/숨은은 모순이다."""
    text = (REPO / "completions" / "_macosctl").read_text()

    assert "_complete-names" in text
    for wrong in ("hidden", "숨은", "숨겨진"):
        assert wrong not in text, f"completion이 _complete-names를 '{wrong}'이라 부른다"


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
