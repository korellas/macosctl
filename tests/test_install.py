"""install.sh staging/dry-run tests (D2-6, D11)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "install.sh"
EXPECTED_OLD_CLI_TARGET = "/example/pre-rename/svc/bin/svc"


MANIFEST = '''\
# legacy single-file manifest
[defaults]
user = "tester"
working_directory = "/srv/app"
log_dir = "/tmp/services"
throttle_seconds = 10
log_max_mb = 20
log_keep = 5
log_rotate_interval_seconds = 900
path = "/usr/bin:/bin"

[[service]]
name = "demo"
label = "com.korellas.demo"
port = 4321
group = "infra"
exec = ["/bin/echo", "ok"]
depends_on = []

[[service]]
name = "webtop"
label = "com.webtop"
port = 7890
group = "dashboard"
exec = ["/bin/echo", "web"]
depends_on = []
'''


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, bytes | str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        info = path.lstat()
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path), stat.S_IMODE(info.st_mode))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
        else:
            result[relative] = ("dir", b"", stat.S_IMODE(info.st_mode))
    return result


def _run(
    manifest: Path,
    staging_root: Path,
    *args: str,
    installer: Path = INSTALLER,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    staging_argument = staging_root
    if ".." not in staging_root.parts and not staging_root.is_symlink():
        # macOS spells TemporaryDirectory through the root-owned /var symlink.
        # Ordinary tests use its canonical /private path; explicit symlink and
        # traversal tests preserve the user spelling to exercise rejection.
        staging_argument = staging_root.resolve(strict=False)
    return subprocess.run(
        [
            "/bin/bash",
            str(installer),
            "--manifest",
            str(manifest),
            "--staging-root",
            str(staging_argument),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_install_writes_policy_before_replacing_svcctl():
    """역순이면 policy 없는 svcctl이 fail-closed로 전부 거부한다."""
    src = INSTALLER.read_text()
    assert src.index("policy.json") < src.index("/usr/local/sbin/macosctl-helper"), \
        "svcctl 교체가 policy 쓰기보다 먼저다 — 제어 공백이 생긴다"


def test_install_requires_explicit_manifest_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve() / "staging"
        result = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--staging-root", str(root)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "--manifest" in result.stderr, result.stderr
        assert not root.exists()


def _write_old_namespace(root: Path) -> None:
    config = root / "etc" / "svc"
    conf = config / "conf.d"
    state = root / "var" / "db" / "svc"
    conf.mkdir(parents=True)
    state.mkdir(parents=True)
    (config / "policy.json").write_text(
        '{"schema":1,"label_prefix":"com.korellas.",'
        '"label_exceptions":{},"service_user":"tester",'
        '"groups":["infra"]}\n'
    )
    (config / "svc.toml").write_text(
        'schema = 1\n\n[defaults]\nworking_directory = "/tmp"\n'
        'log_dir = "/tmp"\npath = "/usr/bin:/bin"\n'
    )
    (conf / "30-demo.toml").write_text(
        'schema = 1\n\n[[service]]\nname = "demo"\n'
        'label = "com.korellas.demo"\nport = 4321\ngroup = "infra"\n'
        'exec = ["/bin/echo", "ok"]\ndepends_on = []\n'
    )
    (state / "inventory").write_text('{"version":2,"labels":{}}\n')
    (state / "state").write_text('{"version":1,"services":{}}\n')
    (state / "lock").write_bytes(b"")
    cli = root / "usr" / "local" / "bin" / "svc"
    cli.parent.mkdir(parents=True)
    os.symlink(EXPECTED_OLD_CLI_TARGET, cli)
    helper = root / "usr" / "local" / "sbin" / "svcctl"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"legacy helper\n")
    helper.chmod(0o755)
    sudoers = root / "etc" / "sudoers.d" / "svcctl"
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("tester ALL=(root) NOPASSWD: /usr/local/sbin/svcctl\n")
    sudoers.chmod(0o440)


def test_migrate_namespace_installs_new_integration_and_preserves_old_staging_tree():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        root.mkdir()
        _write_old_namespace(root)
        manifest = base / "unused-services.toml"
        before_old_config = _snapshot(root / "etc" / "svc")
        before_old_state = _snapshot(root / "var" / "db" / "svc")

        migration_args = (
            "--migrate-namespace",
            "--expected-old-cli-target",
            EXPECTED_OLD_CLI_TARGET,
        )
        result = _run(manifest, root, *migration_args)

        assert result.returncode == 0, result.stderr
        assert _snapshot(root / "etc" / "svc") == before_old_config
        assert _snapshot(root / "var" / "db" / "svc") == before_old_state
        assert (root / "etc" / "macosctl" / "macosctl.toml").exists()
        assert (root / "var" / "db" / "macosctl" / "inventory").exists()
        assert (root / "usr" / "local" / "sbin" / "macosctl-helper").exists()
        assert (root / "etc" / "sudoers.d" / "macosctl").exists()
        cli = root / "usr" / "local" / "bin" / "macosctl"
        assert cli.is_symlink() and cli.resolve() == REPO / "bin" / "macosctl"
        offsets = [result.stdout.index(f"{step}") for step in ("①", "②", "③", "④", "⑤")]
        assert offsets == sorted(offsets)

        second = _run(manifest, root, *migration_args)
        assert second.returncode == 0, second.stderr
        assert _snapshot(root / "etc" / "svc") == before_old_config
        assert _snapshot(root / "var" / "db" / "svc") == before_old_state


def test_migrate_namespace_dry_run_is_zero_write():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        root.mkdir()
        _write_old_namespace(root)
        before = _snapshot(root)
        result = _run(
            base / "unused.toml",
            root,
            "--migrate-namespace",
            "--expected-old-cli-target",
            EXPECTED_OLD_CLI_TARGET,
            "--dry-run",
        )
        assert result.returncode == 0, result.stderr
        assert _snapshot(root) == before
        assert "⑥" in result.stdout


def test_migrate_namespace_requires_the_expected_old_cli_target():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        root.mkdir()
        _write_old_namespace(root)
        before = _snapshot(root)

        result = _run(base / "unused.toml", root, "--migrate-namespace", "--dry-run")

        assert result.returncode != 0
        assert "--expected-old-cli-target" in result.stderr
        assert _snapshot(root) == before


def test_expected_old_cli_target_is_rejected_without_migration_mode():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        root.mkdir()
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)

        result = _run(
            manifest,
            root,
            "--expected-old-cli-target",
            EXPECTED_OLD_CLI_TARGET,
            "--dry-run",
        )

        assert result.returncode != 0
        assert "--migrate-namespace에만" in result.stderr


def test_migration_dry_run_rejects_a_different_old_cli_target_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "root"
        root.mkdir()
        _write_old_namespace(root)
        before = _snapshot(root)

        result = _run(
            base / "unused.toml",
            root,
            "--migrate-namespace",
            "--expected-old-cli-target",
            "/different/svc",
            "--dry-run",
        )

        assert result.returncode != 0
        assert "foreign old cli" in result.stderr
        assert _snapshot(root) == before


def test_installer_contains_no_historical_old_cli_target():
    source = INSTALLER.read_text()
    assert "/Users/example/project/bin/svc" not in source
    assert "/Users/example/git/svc/bin/svc" not in source


def test_dry_run_is_read_only_and_reports_internal_order():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        root.mkdir()
        marker = root / "OWNER-DATA"
        marker.write_text("keep")
        before_root = _snapshot(root)
        before_manifest = manifest.read_bytes()

        result = _run(manifest, root, "--dry-run")

        assert result.returncode == 0, result.stderr
        assert _snapshot(root) == before_root
        assert manifest.read_bytes() == before_manifest
        ordered = (
            "policy.json",
            "macosctl.toml",
            "conf.d/30-ai.toml",
            "/usr/local/sbin/macosctl-helper",
            "/usr/local/bin/macosctl",
        )
        offsets = [result.stdout.index(value) for value in ordered]
        assert offsets == sorted(offsets), result.stdout


def test_conf_dir_is_not_created_until_the_manifest_split_succeeds():
    """실제 mutation도 policy → split → conf.d 순서이며 로그 순서만 맞지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        fake_bin = base / "fake-bin"
        fake_bin.mkdir()
        counter = base / "svc-toml-python-count"
        wrapper = fake_bin / "python3"
        wrapper.write_text(f'''#!/bin/bash
if [[ "${{3##*/}}" == "macosctl.toml" ]]; then
  n=0; [[ ! -e "{counter}" ]] || n=$(<"{counter}")
  n=$((n + 1)); printf '%s' "$n" > "{counter}"
  if [[ "$n" -eq 3 ]]; then exit 91; fi
fi
exec "{sys.executable}" "$@"
''')
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = _run(manifest, root, env=env)

        assert result.returncode != 0
        assert (root / "etc" / "macosctl" / "policy.json").exists()
        assert not (root / "etc" / "macosctl" / "conf.d").exists(), \
            "split 실패 전에 Step ③ conf.d를 만들었다"
        assert manifest.read_text() == MANIFEST


def test_staged_install_splits_manifest_and_sets_modes():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"

        result = _run(manifest, root)

        assert result.returncode == 0, result.stderr
        config = root / "etc" / "macosctl"
        policy = config / "policy.json"
        defaults = config / "macosctl.toml"
        fragment = config / "conf.d" / "30-ai.toml"
        installed_svcctl = root / "usr" / "local" / "sbin" / "macosctl-helper"
        sudoers = root / "etc" / "sudoers.d" / "macosctl"
        cli = root / "usr" / "local" / "bin" / "macosctl"

        assert stat.S_IMODE(config.stat().st_mode) == 0o755
        assert stat.S_IMODE(policy.stat().st_mode) == 0o644
        assert stat.S_IMODE(defaults.stat().st_mode) == 0o644
        assert stat.S_IMODE((config / "conf.d").stat().st_mode) == 0o755
        assert stat.S_IMODE(installed_svcctl.stat().st_mode) == 0o755
        assert stat.S_IMODE(sudoers.stat().st_mode) == 0o440
        assert stat.S_IMODE((root / "var" / "db" / "macosctl").stat().st_mode) == 0o755
        assert fragment.is_symlink() and fragment.resolve() == manifest.resolve()
        assert cli.is_symlink() and cli.resolve() == REPO / "bin" / "macosctl"
        assert installed_svcctl.read_bytes() == (REPO / "sbin" / "macosctl-helper").read_bytes()

        policy_data = __import__("json").loads(policy.read_text())
        assert policy_data == {
            "schema": 1,
            "label_prefix": "com.korellas.",
            "label_exceptions": {"webtop": "com.webtop"},
            "service_user": "tester",
            "groups": ["infra", "dashboard"],
        }
        defaults_data = tomllib.loads(defaults.read_text())
        assert defaults_data["schema"] == 1
        assert "user" not in defaults_data["defaults"]
        assert defaults_data["defaults"]["log_max_mb"] == 20
        rewritten = tomllib.loads(manifest.read_text())
        assert rewritten["schema"] == 1
        assert "defaults" not in rewritten
        assert [service["name"] for service in rewritten["service"]] == [
            "demo", "webtop",
        ]
        assert "tester ALL=(root) NOPASSWD: /usr/local/sbin/macosctl-helper" in sudoers.read_text()
        assert "*" not in sudoers.read_text()


def test_staged_install_is_idempotent_after_manifest_was_split():
    """policy 뒤에서 끊긴 설치는 defaults 없는 fragment로 안전하게 재개한다."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        first = _run(manifest, root)
        assert first.returncode == 0, first.stderr
        before_root = _snapshot(root)
        before_manifest = manifest.read_bytes()

        second = _run(manifest, root)

        assert second.returncode == 0, second.stderr
        assert _snapshot(root) == before_root
        assert manifest.read_bytes() == before_manifest


def test_install_refuses_a_foreign_cli_symlink_without_replacing_it():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        cli = root / "usr" / "local" / "bin" / "macosctl"
        cli.parent.mkdir(parents=True)
        foreign = base / "OWNER-CLI"
        foreign.write_text("owner")
        os.symlink(foreign, cli)

        result = _run(manifest, root)

        assert result.returncode != 0
        assert cli.is_symlink() and cli.resolve() == foreign.resolve()
        assert foreign.read_text() == "owner"


def test_install_refuses_a_foreign_fragment_before_splitting_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        fragment = root / "etc" / "macosctl" / "conf.d" / "30-ai.toml"
        fragment.parent.mkdir(parents=True)
        fragment.write_text("OWNER FRAGMENT")
        before = manifest.read_bytes()

        result = _run(manifest, root)

        assert result.returncode != 0
        assert fragment.read_text() == "OWNER FRAGMENT"
        assert manifest.read_bytes() == before
        assert not (root / "etc" / "macosctl" / "policy.json").exists(), \
            "fragment 충돌을 첫 mutation 뒤에 발견했다"


def test_install_refuses_existing_human_svc_toml_before_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        defaults = root / "etc" / "macosctl" / "macosctl.toml"
        defaults.parent.mkdir(parents=True)
        defaults.write_text('schema = 1\n[defaults]\nlog_dir = "/OWNER"\n')

        result = _run(manifest, root)

        assert result.returncode != 0
        assert defaults.read_text() == 'schema = 1\n[defaults]\nlog_dir = "/OWNER"\n'
        assert manifest.read_text() == MANIFEST
        assert not (root / "etc" / "macosctl" / "policy.json").exists()


def test_staging_root_rejects_dotdot_and_symlink_escape():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        outside = base / "outside"
        outside.mkdir()
        linked = base / "linked-root"
        os.symlink(outside, linked)

        for staging_root in (base / "stage" / ".." / "outside", linked):
            before = _snapshot(outside)
            result = _run(manifest, staging_root)
            assert result.returncode != 0, staging_root
            assert _snapshot(outside) == before, staging_root
            assert manifest.read_text() == MANIFEST


def test_staging_root_rejects_symlink_in_a_fixed_destination_parent():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        root.mkdir()
        outside = base / "OWNER-OUTSIDE"
        outside.mkdir()
        os.symlink(outside, root / "etc")
        before = _snapshot(outside)

        result = _run(manifest, root)

        assert result.returncode != 0
        assert _snapshot(outside) == before
        assert manifest.read_text() == MANIFEST


def test_fragment_publish_does_not_follow_conf_dir_swap_after_preflight():
    """Step ③은 검사한 conf.d fd에만 쓰며 path 재개방 race를 허용하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        config = root / "etc" / "macosctl"
        conf = config / "conf.d"
        conf.mkdir(parents=True)
        outside = base / "OWNER-OUTSIDE"
        outside.mkdir()
        swapped = threading.Event()

        def swap_after_policy():
            policy = config / "policy.json"
            deadline = time.monotonic() + 5
            while not policy.exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            if not policy.exists():
                return
            conf.rename(config / "conf.original")
            os.symlink(outside, conf)
            swapped.set()

        racer = threading.Thread(target=swap_after_policy)
        racer.start()
        result = _run(manifest, root)
        racer.join(timeout=5)

        assert swapped.is_set(), "race fixture가 conf.d를 교체하지 못했다"
        assert result.returncode != 0, result.stdout
        assert not (outside / "30-ai.toml").exists(), \
            "교체된 conf.d symlink를 따라 외부에 fragment를 썼다"


def test_installs_the_same_svcctl_bytes_that_passed_audit():
    """사용자 repo 원본이 audit 직후 바뀌어도 검증된 snapshot만 설치한다."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fake_repo = base / "repo"
        (fake_repo / "sbin").mkdir(parents=True)
        (fake_repo / "bin").mkdir()
        shutil.copy2(INSTALLER, fake_repo / "install.sh")
        shutil.copy2(REPO / "bin" / "macosctl", fake_repo / "bin" / "macosctl")
        source = fake_repo / "sbin" / "macosctl-helper"
        shutil.copy2(REPO / "sbin" / "macosctl-helper", source)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"

        fake_bin = base / "fake-bin"
        fake_bin.mkdir()
        marker = base / "mutated"
        wrapper = fake_bin / "python3"
        wrapper.write_text(f'''#!/bin/bash
"{sys.executable}" "$@"
rc=$?
if [[ "$rc" -eq 0 && "${{1:-}}" == "-" && "${{2##*/}}" == "macosctl-helper" && ! -e "{marker}" ]]; then
  printf '\n# OWNER_RACE\nPath("/Users/owner/payload").read_text()\n' >> "{source}"
  touch "{marker}"
fi
exit "$rc"
''')
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = subprocess.run(
            [
                "/bin/bash", str(fake_repo / "install.sh"),
                "--manifest", str(manifest),
                "--staging-root", str(root.resolve(strict=False)),
            ],
            capture_output=True, text=True, check=False, env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "OWNER_RACE" in source.read_text(), "audit 뒤 race가 실행되지 않았다"
        installed = root / "usr" / "local" / "sbin" / "macosctl-helper"
        assert "OWNER_RACE" not in installed.read_text(), \
            "audit하지 않은 repo bytes를 다시 열어 설치했다"


def test_install_audit_rejects_user_writable_path_reads_before_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fake_repo = base / "repo"
        (fake_repo / "sbin").mkdir(parents=True)
        (fake_repo / "bin").mkdir()
        installer = fake_repo / "install.sh"
        shutil.copy2(INSTALLER, installer)
        shutil.copy2(REPO / "bin" / "macosctl", fake_repo / "bin" / "macosctl")
        source = (REPO / "sbin" / "macosctl-helper").read_text()
        source += '\nPath("/Users/attacker/payload").read_text()\n'
        (fake_repo / "sbin" / "macosctl-helper").write_text(source)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"

        result = _run(manifest, root, installer=installer)

        assert result.returncode != 0
        assert "사용자 쓰기 가능 경로" in result.stderr
        assert not root.exists(), "감사 실패 전에 설치물을 만들었다"
        assert manifest.read_text() == MANIFEST


def test_install_audit_rejects_relative_and_home_driven_reads():
    payloads = (
        'Path("repo-payload").read_text()',
        'Path(os.environ["HOME"]).read_text()',
        'Path(os.environ["EVIL_PATH"]).read_text()',
        'Path(sys.argv[-1]).read_text()',
    )
    for index, payload in enumerate(payloads):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_repo = base / "repo"
            (fake_repo / "sbin").mkdir(parents=True)
            (fake_repo / "bin").mkdir()
            installer = fake_repo / "install.sh"
            shutil.copy2(INSTALLER, installer)
            shutil.copy2(REPO / "bin" / "macosctl", fake_repo / "bin" / "macosctl")
            source = (REPO / "sbin" / "macosctl-helper").read_text()
            (fake_repo / "sbin" / "macosctl-helper").write_text(source + f"\n{payload}\n")
            manifest = base / "services.toml"
            manifest.write_text(MANIFEST)
            root = base / f"root-{index}"

            result = _run(manifest, root, installer=installer)

            assert result.returncode != 0, payload
            assert "사용자 쓰기 가능 경로" in result.stderr
            assert not root.exists(), "감사 실패 전에 설치물을 만들었다"


def test_install_audit_rejects_dynamic_code_loading_and_relative_os_open():
    payloads = (
        'import importlib\nimportlib.import_module("payload")',
        'import runpy\nrunpy.run_path("payload.py")',
        'eval("1 + 1")',
        'exec("value = 1")',
        'compile("value = 1", "payload", "exec")',
        '__import__("payload")',
        'os.open("payload", os.O_RDONLY)',
        'import pathlib\npathlib.Path("repo-payload").read_text()',
        'from pathlib import Path as P\nP("repo-payload").read_text()',
        'import os as operating\noperating.open("payload", operating.O_RDONLY)',
        'from os import open as oopen\noopen("payload", os.O_RDONLY)',
        'getattr(Path, "cwd")().joinpath("payload").read_text()',
    )
    for index, payload in enumerate(payloads):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_repo = base / "repo"
            (fake_repo / "sbin").mkdir(parents=True)
            (fake_repo / "bin").mkdir()
            installer = fake_repo / "install.sh"
            shutil.copy2(INSTALLER, installer)
            shutil.copy2(REPO / "bin" / "macosctl", fake_repo / "bin" / "macosctl")
            source = (REPO / "sbin" / "macosctl-helper").read_text()
            (fake_repo / "sbin" / "macosctl-helper").write_text(source + f"\n{payload}\n")
            manifest = base / "services.toml"
            manifest.write_text(MANIFEST)
            root = base / f"root-{index}"

            result = _run(manifest, root, installer=installer)

            assert result.returncode != 0, payload
            assert not root.exists(), "동적 로딩 감사 실패 전에 설치물을 만들었다"


def test_uninstall_preserves_configuration_state_and_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        installed = _run(manifest, root)
        assert installed.returncode == 0, installed.stderr
        config = root / "etc" / "macosctl"
        state_dir = root / "var" / "db" / "macosctl"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state").write_text("OWNER STATE")
        rewritten = manifest.read_bytes()
        defaults = (config / "macosctl.toml").read_bytes()

        result = _run(manifest, root, "--uninstall")

        assert result.returncode == 0, result.stderr
        assert not os.path.lexists(root / "usr" / "local" / "bin" / "macosctl")
        assert not os.path.lexists(root / "usr" / "local" / "sbin" / "macosctl-helper")
        assert not os.path.lexists(root / "etc" / "sudoers.d" / "macosctl")
        assert config.exists(), "사람이 관리하는 설정을 uninstall이 지웠다"
        assert (config / "policy.json").exists()
        assert (config / "macosctl.toml").read_bytes() == defaults
        assert manifest.read_bytes() == rewritten
        assert (state_dir / "state").read_text() == "OWNER STATE"


def test_uninstall_refuses_foreign_cli_before_removing_anything():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        manifest = base / "services.toml"
        manifest.write_text(MANIFEST)
        root = base / "root"
        installed = _run(manifest, root)
        assert installed.returncode == 0, installed.stderr
        cli = root / "usr" / "local" / "bin" / "macosctl"
        cli.unlink()
        foreign = base / "OWNER-CLI"
        foreign.write_text("owner")
        os.symlink(foreign, cli)

        result = _run(manifest, root, "--uninstall")

        assert result.returncode != 0
        assert cli.is_symlink() and cli.resolve() == foreign.resolve()
        assert (root / "usr" / "local" / "sbin" / "macosctl-helper").exists(), \
            "사전 검증 전에 부분 uninstall했다"
        assert (root / "etc" / "sudoers.d" / "macosctl").exists()


def test_uninstall_refuses_foreign_privileged_files_before_removing_anything():
    for relative in (
        Path("usr/local/sbin/macosctl-helper"),
        Path("etc/sudoers.d/macosctl"),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = base / "services.toml"
            manifest.write_text(MANIFEST)
            root = base / "root"
            installed = _run(manifest, root)
            assert installed.returncode == 0, installed.stderr
            foreign = root / relative
            foreign.chmod(0o644)
            foreign.write_text("OWNER DATA")
            cli = root / "usr" / "local" / "bin" / "macosctl"

            result = _run(manifest, root, "--uninstall")

            assert result.returncode != 0, relative
            assert foreign.read_text() == "OWNER DATA"
            assert cli.is_symlink(), "사전 검증 전에 CLI를 부분 삭제했다"
            sibling = (
                root / "etc" / "sudoers.d" / "macosctl"
                if "sbin" in relative.parts
                else root / "usr" / "local" / "sbin" / "macosctl-helper"
            )
            assert sibling.exists(), "사전 검증 전에 다른 privileged 파일을 삭제했다"


def test_cli_guidance_names_the_consolidated_installer():
    source = (REPO / "bin" / "macosctl").read_text()
    assert "scripts/install-svcctl.sh" not in source
    assert "install.sh" in source


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
