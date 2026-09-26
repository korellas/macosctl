"""log-rotate 셸 소비자의 설정 경로 계약 테스트."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "libexec" / "rotate-service-logs.sh"


def _run(config: Path | None):
    env = os.environ.copy()
    env.pop("MANIFEST", None)
    env.pop("MACOSCTL_CONFIG", None)
    if config is not None:
        env["MACOSCTL_CONFIG"] = str(config)
    return subprocess.run(
        [str(SCRIPT), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_reads_defaults_from_plist_supplied_config():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        logs = root / "logs"
        logs.mkdir()
        config = root / "macosctl.toml"
        config.write_text(
            "[defaults]\n"
            f"log_dir = {json.dumps(str(logs))}\n"
            "log_max_mb = 23\n"
            "log_keep = 4\n"
        )

        result = _run(config)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "threshold 23MB, keep 4" in result.stdout


def test_refuses_to_guess_config_path_when_environment_is_missing():
    result = _run(None)

    assert result.returncode == 1
    assert "MACOSCTL_CONFIG" in result.stderr


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
