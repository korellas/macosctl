#!/usr/bin/env bash
# macosctl installer — D2-6 ownership and D11 cutover order.
#
# The first occurrence of the two privileged artifacts is intentionally in
# their real install order.  A source-level regression test protects the
# fail-closed transition: policy.json must exist before the new root wrapper.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SCRIPT_DIR}"

POLICY_REL="/etc/macosctl/policy.json"
SVCCTL_REL="/usr/local/sbin/macosctl-helper"
DEFAULTS_REL="/etc/macosctl/macosctl.toml"
CONF_REL="/etc/macosctl/conf.d"
FRAGMENT_REL="/etc/macosctl/conf.d/30-ai.toml"
SUDOERS_REL="/etc/sudoers.d/macosctl"
CLI_REL="/usr/local/bin/macosctl"

MANIFEST_PATH=""
STAGING_ROOT=""
DRY_RUN=0
UNINSTALL=0
MIGRATE_NAMESPACE=0
EXPECTED_OLD_CLI_TARGET=""

usage() {
    cat <<'EOF'
사용법: install.sh [--dry-run] [--uninstall] [--migrate-namespace]
                  [--expected-old-cli-target PATH]
                  [--manifest PATH] [--staging-root PATH]

  --dry-run           검증과 실행 순서 출력만 하고 파일을 바꾸지 않는다.
  --uninstall         helper/sudoers/CLI 연결만 제거한다. 설정과 state는 보존한다.
  --migrate-namespace 구 svc namespace를 검증해 macosctl로 한 번에 이식한다.
  --expected-old-cli-target PATH
                      이식 시 제거를 허가할 구 svc CLI의 정확한 link target.
  --manifest PATH     분리할 legacy services.toml (일반 설치 시 필수).
  --staging-root PATH 테스트 전용 격리 루트. / 는 허용하지 않는다.
EOF
}

die() {
    printf '[install] 오류: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '[install] %s\n' "$*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --migrate-namespace) MIGRATE_NAMESPACE=1 ;;
        --expected-old-cli-target)
            [[ $# -ge 2 ]] || die "--expected-old-cli-target에는 경로가 필요하다"
            EXPECTED_OLD_CLI_TARGET="$2"
            shift
            ;;
        --manifest)
            [[ $# -ge 2 ]] || die "--manifest에는 경로가 필요하다"
            MANIFEST_PATH="$2"
            shift
            ;;
        --staging-root)
            [[ $# -ge 2 ]] || die "--staging-root에는 경로가 필요하다"
            STAGING_ROOT="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "알 수 없는 인자: $1"
            ;;
    esac
    shift
done

if [[ "$MIGRATE_NAMESPACE" -eq 1 ]]; then
    [[ -n "$EXPECTED_OLD_CLI_TARGET" ]] \
        || die "--migrate-namespace에는 --expected-old-cli-target이 필요하다"
else
    [[ -z "$EXPECTED_OLD_CLI_TARGET" ]] \
        || die "--expected-old-cli-target은 --migrate-namespace에만 쓸 수 있다"
fi
if [[ -n "$EXPECTED_OLD_CLI_TARGET" ]]; then
    [[ "$EXPECTED_OLD_CLI_TARGET" == /* ]] \
        || die "--expected-old-cli-target은 절대 경로여야 한다"
    [[ "$EXPECTED_OLD_CLI_TARGET" != *$'\n'* \
       && "$EXPECTED_OLD_CLI_TARGET" != *$'\r'* ]] \
        || die "--expected-old-cli-target에는 개행을 쓸 수 없다"
fi

if [[ -n "$STAGING_ROOT" ]]; then
    [[ "$STAGING_ROOT" = /* ]] || die "--staging-root는 절대 경로여야 한다"
    [[ "$STAGING_ROOT" != "/" ]] || die "--staging-root / 는 허용하지 않는다"
    [[ "/${STAGING_ROOT#/}/" != *"/../"* ]] \
        || die "--staging-root에 .. 를 쓸 수 없다"
    STAGING_ROOT="$(python3 - "$STAGING_ROOT" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw = Path(sys.argv[1])
probe = Path("/")
for part in raw.parts[1:]:
    probe = probe / part
    try:
        info = probe.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"staging-root 경로의 심링크는 허용하지 않는다: {probe}")
    if probe == raw and not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"staging-root가 실제 디렉터리가 아니다: {probe}")
resolved = os.path.realpath(raw)
if resolved == "/":
    raise SystemExit("staging-root가 / 로 해석되어 허용하지 않는다")
print(resolved)
PY
)" || die "--staging-root를 안전하게 확정할 수 없다"
fi

target() {
    printf '%s%s' "$STAGING_ROOT" "$1"
}

POLICY_PATH="$(target "$POLICY_REL")"
SVCCTL_PATH="$(target "$SVCCTL_REL")"
DEFAULTS_PATH="$(target "$DEFAULTS_REL")"
CONF_PATH="$(target "$CONF_REL")"
FRAGMENT_PATH="$(target "$FRAGMENT_REL")"
SUDOERS_PATH="$(target "$SUDOERS_REL")"
CLI_PATH="$(target "$CLI_REL")"
SVCCTL_SOURCE="${REPO}/sbin/macosctl-helper"
CLI_SOURCE="${REPO}/bin/macosctl"

if [[ -n "$STAGING_ROOT" ]]; then
    python3 - "$STAGING_ROOT" \
        "$POLICY_PATH" "$DEFAULTS_PATH" "$FRAGMENT_PATH" \
        "$SVCCTL_PATH" "$SUDOERS_PATH" "$CLI_PATH" \
        "$(target "/var/db/macosctl/inventory")" <<'PY' \
        || die "staging-root 안의 고정 설치 경로가 안전하지 않다"
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
for raw_target in sys.argv[2:]:
    target = Path(raw_target)
    try:
        relative_parent = target.parent.relative_to(root)
    except ValueError:
        raise SystemExit(f"설치 경로가 staging-root 밖이다: {target}")
    current = root
    for part in relative_parent.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"설치 경로 parent가 심링크다: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit(f"설치 경로 parent가 디렉터리가 아니다: {current}")
PY
fi

require_privilege() {
    if [[ -z "$STAGING_ROOT" && "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
        die "root가 필요하다: sudo ${REPO}/install.sh"
    fi
}

remove_integration() {
    local path
    # Resolve every target before removing the first one.  A path replaced by
    # another owner turns uninstall into a refusal, never a partial deletion.
    if [[ -L "$CLI_PATH" ]]; then
        [[ "$(readlink "$CLI_PATH")" == "$CLI_SOURCE" ]] \
            || die "다른 CLI 링크라 제거하지 않는다: $CLI_PATH"
    elif [[ -e "$CLI_PATH" ]]; then
        die "CLI 경로가 installer 소유 심링크가 아니다: $CLI_PATH"
    fi
    for path in "$SUDOERS_PATH" "$SVCCTL_PATH"; do
        if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
            die "예상하지 않은 파일 형식이라 제거하지 않는다: $path"
        fi
    done
    if [[ -f "$SVCCTL_PATH" ]] && ! cmp -s "$SVCCTL_SOURCE" "$SVCCTL_PATH"; then
        die "설치본과 다른 macosctl-helper라 제거하지 않는다: $SVCCTL_PATH"
    fi
    if [[ -f "$SUDOERS_PATH" ]]; then
        python3 - "$SUDOERS_PATH" "$POLICY_PATH" <<'PY' \
            || die "canonical sudoers가 아니라 제거하지 않는다: $SUDOERS_PATH"
import json
import re
import sys
from pathlib import Path

sudoers = Path(sys.argv[1])
policy = Path(sys.argv[2])
try:
    data = json.loads(policy.read_text())
    user = data["service_user"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(user, str) or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) is None:
    raise SystemExit(1)
expected = f"{user} ALL=(root) NOPASSWD: /usr/local/sbin/macosctl-helper\n".encode()
try:
    actual = sudoers.read_bytes()
except OSError:
    raise SystemExit(1)
raise SystemExit(0 if actual == expected else 1)
PY
    fi
    for path in "$CLI_PATH" "$SUDOERS_PATH" "$SVCCTL_PATH"; do
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "dry-run 제거 $path"
            continue
        fi
        if [[ -L "$path" || -f "$path" ]]; then
            rm -f -- "$path"
            log "제거 $path"
        elif [[ -e "$path" ]]; then
            die "예상하지 않은 파일 형식이라 제거하지 않는다: $path"
        fi
    done
    log "설정 ${POLICY_PATH%/policy.json} 및 /var/db/macosctl은 보존했다."
}

require_privilege
if [[ "$UNINSTALL" -eq 1 ]]; then
    remove_integration
    exit 0
fi

[[ -f "$SVCCTL_SOURCE" ]] || die "macosctl-helper 원본이 없다: $SVCCTL_SOURCE"
[[ -f "$CLI_SOURCE" ]] || die "macosctl 원본이 없다: $CLI_SOURCE"
if [[ "$MIGRATE_NAMESPACE" -eq 0 ]]; then
    [[ -n "$MANIFEST_PATH" ]] || die "일반 설치에는 --manifest PATH가 필요하다"
    [[ -f "$MANIFEST_PATH" ]] || die "매니페스트가 없다: $MANIFEST_PATH"
fi

SVCCTL_SNAPSHOT_DIR=""
cleanup_snapshot() {
    if [[ "$SVCCTL_SNAPSHOT_DIR" == /tmp/macosctl-install.* \
          && -d "$SVCCTL_SNAPSHOT_DIR" ]]; then
        /bin/rm -rf -- "$SVCCTL_SNAPSHOT_DIR"
    fi
}
trap cleanup_snapshot EXIT INT TERM

SVCCTL_AUDIT_PATH="$SVCCTL_SOURCE"
if [[ "$DRY_RUN" -eq 0 ]]; then
    SVCCTL_SNAPSHOT_DIR="$(mktemp -d /tmp/macosctl-install.XXXXXX)" \
        || die "macosctl-helper 감사 snapshot을 만들 수 없다"
    SVCCTL_AUDIT_PATH="${SVCCTL_SNAPSHOT_DIR}/macosctl-helper"
    /bin/cp "$SVCCTL_SOURCE" "$SVCCTL_AUDIT_PATH" \
        || die "macosctl-helper 감사 snapshot을 복사할 수 없다"
    /bin/chmod 0600 "$SVCCTL_AUDIT_PATH"
fi

# Root로 복사될 프로그램은 stdlib 외 코드를 import하거나 사용자 쓰기 가능 경로에서
# 런타임 데이터를 읽어서는 안 된다.  기존 import grep을 direct path literals,
# HOME/cwd/expanduser, __file__/sys.path 의존까지 확장한다.
audit_svcctl() {
    python3 - "$SVCCTL_AUDIT_PATH" <<'PY'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
except (OSError, SyntaxError, UnicodeError) as exc:
    raise SystemExit(f"svcctl 구문/읽기 감사 실패: {exc}")

stdlib = set(sys.stdlib_module_names)
forbidden_stdlib_loaders = {"importlib", "runpy"}
allowed_paths = {
    "/Library/LaunchDaemons",
    "/etc/macosctl/policy.json",
    "/var/db/macosctl/inventory",
    "/var/db/macosctl/state",
    "/var/db/macosctl/lock",
    "/var/log/macosctl-helper.log",
    "/bin/launchctl",
    # 부팅 세션 UUID의 출처. launchctl과 같은 등급의 root 소유 시스템 바이너리라
    # 사용자 쓰기 가능 경로 금지 규칙을 흔들지 않는다. 정지 의도를 "이번 부팅"으로
    # 한정하려면 helper 자신이 이 값을 읽어야 한다.
    "/usr/sbin/sysctl",
}

def is_os_environ(node):
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in os_module_names
        and node.attr == "environ"
    )

def constant_string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

path_constructor_names = set()
pathlib_module_names = set()
os_module_names = set()
os_open_names = set()
for imported in ast.walk(tree):
    if isinstance(imported, ast.Import):
        for alias in imported.names:
            if alias.name == "pathlib":
                pathlib_module_names.add(alias.asname or "pathlib")
            if alias.name == "os":
                os_module_names.add(alias.asname or "os")
    elif isinstance(imported, ast.ImportFrom) and imported.module == "pathlib":
        for alias in imported.names:
            if alias.name == "Path":
                path_constructor_names.add(alias.asname or "Path")
    elif isinstance(imported, ast.ImportFrom) and imported.module == "os":
        for alias in imported.names:
            if alias.name == "open":
                os_open_names.add(alias.asname or "open")

def is_path_constructor(node):
    return (
        isinstance(node, ast.Name) and node.id in path_constructor_names
    ) or (
        isinstance(node, ast.Attribute)
        and node.attr == "Path"
        and isinstance(node.value, ast.Name)
        and node.value.id in pathlib_module_names
    )

def is_os_open(node):
    return (
        isinstance(node, ast.Name) and node.id in os_open_names
    ) or (
        isinstance(node, ast.Attribute)
        and node.attr == "open"
        and isinstance(node.value, ast.Name)
        and node.value.id in os_module_names
    )

for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        modules = [alias.name.split(".", 1)[0] for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        modules = [] if node.module == "__future__" else [
            (node.module or "").split(".", 1)[0]
        ]
    else:
        modules = []
    for module in modules:
        if not module or module not in stdlib:
            raise SystemExit(
                f"svcctl 자기완결형 감사 실패: 외부/레포 import {module!r}"
            )
        if module in forbidden_stdlib_loaders:
            raise SystemExit(
                f"svcctl 자기완결형 감사 실패: 동적 로더 import {module!r}"
            )

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
        if value.startswith("/") and value not in allowed_paths:
            raise SystemExit(
                "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: "
                f"허용되지 않은 절대 경로 {value!r}"
            )
        if value in {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "TMPDIR"}:
            raise SystemExit(
                "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: "
                f"환경 경로 {value!r}"
            )

    if (
        isinstance(node, ast.Call)
        and is_path_constructor(node.func)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and not node.args[0].value.startswith("/")
    ):
        raise SystemExit(
            "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: "
            f"상대 경로 {node.args[0].value!r}"
        )

    if isinstance(node, ast.Call) and is_path_constructor(node.func):
        argument = node.args[0] if node.args else None
        if argument is not None and not isinstance(argument, (ast.Constant, ast.Name)):
            raise SystemExit(
                "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: dynamic Path"
            )
        if isinstance(argument, ast.Name) and argument.id != "path":
            raise SystemExit(
                "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: dynamic Path"
            )

    environment_key = None
    if isinstance(node, ast.Subscript) and is_os_environ(node.value):
        environment_key = constant_string(node.slice)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and is_os_environ(node.func.value)
        and node.args
    ):
        environment_key = constant_string(node.args[0])
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in os_module_names
        and node.func.attr == "getenv"
        and node.args
    ):
        environment_key = constant_string(node.args[0])
    if environment_key is not None and environment_key != "SUDO_USER":
        raise SystemExit(
            "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: "
            f"환경 입력 {environment_key!r}"
        )

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ):
        raise SystemExit("svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: dynamic open")

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {
            "eval", "exec", "compile", "__import__", "getattr",
        }
    ):
        raise SystemExit(
            "svcctl 자기완결형 감사 실패: 동적 코드 실행 " + node.func.id
        )

    if (
        isinstance(node, ast.Call)
        and is_os_open(node.func)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and not node.args[0].value.startswith("/")
        and not any(keyword.arg == "dir_fd" for keyword in node.keywords)
    ):
        raise SystemExit(
            "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: relative os.open"
        )

    if isinstance(node, ast.Name) and node.id == "__file__":
        raise SystemExit("svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: __file__")
    if isinstance(node, ast.Attribute) and node.attr in {
        "home", "cwd", "expanduser",
    }:
        raise SystemExit(
            "svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: " + node.attr
        )
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    ):
        raise SystemExit("svcctl 사용자 쓰기 가능 경로 읽기 감사 실패: sys.path")
PY
}

# Parse the real legacy manifest before the first mutation.  Besides catching TOML errors,
# this proves that the split has a service user and that every policy field is representable.
validate_manifest() {
    python3 - "$MANIFEST_PATH" "$DEFAULTS_PATH" "$POLICY_PATH" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
defaults_path = Path(sys.argv[2])
policy_path = Path(sys.argv[3])
try:
    data = tomllib.loads(path.read_text())
except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
    raise SystemExit(f"매니페스트 검증 실패 ({path}): {exc}")
if "schema" in data and data["schema"] != 1:
    raise SystemExit(f"매니페스트 검증 실패 ({path}): schema는 1이어야 한다")
defaults = data.get("defaults")
if defaults is None:
    try:
        defaults_document = tomllib.loads(defaults_path.read_text())
        policy = json.loads(policy_path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"매니페스트 재설치 검증 실패: 분리된 defaults/policy를 읽을 수 없다: {exc}"
        )
    if defaults_document.get("schema") != 1:
        raise SystemExit("매니페스트 재설치 검증 실패: macosctl.toml schema가 1이 아니다")
    defaults = defaults_document.get("defaults")
    if not isinstance(defaults, dict) or "user" in defaults:
        raise SystemExit("매니페스트 재설치 검증 실패: macosctl.toml defaults가 잘못됐다")
    user = policy.get("service_user") if isinstance(policy, dict) else None
else:
    if not isinstance(defaults, dict):
        raise SystemExit(f"매니페스트 검증 실패 ({path}): [defaults]가 테이블이 아니다")
    user = defaults.get("user")
if not isinstance(user, str) or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) is None:
    raise SystemExit(f"매니페스트 검증 실패 ({path}): service_user가 잘못됐다")
allowed_defaults = {
    "working_directory", "log_dir", "throttle_seconds", "path",
    "log_rotate_interval_seconds", "log_max_mb", "log_keep",
}
if data.get("defaults") is not None:
    allowed_defaults.add("user")
unknown = sorted(set(defaults) - allowed_defaults)
if unknown:
    raise SystemExit(
        f"매니페스트 검증 실패 ({path}): 모르는 defaults 키 {', '.join(unknown)}"
    )
services = data.get("service")
if not isinstance(services, list) or not services:
    raise SystemExit(f"매니페스트 검증 실패 ({path}): [[service]]가 없다")
for service in services:
    if not isinstance(service, dict):
        raise SystemExit(f"매니페스트 검증 실패 ({path}): service가 테이블이 아니다")
    for field in ("name", "label", "group"):
        if not isinstance(service.get(field), str):
            raise SystemExit(
                f"매니페스트 검증 실패 ({path}): service.{field}가 잘못됐다"
            )
    name = service["name"]
    label = service["label"]
    group = service["group"]
    if re.fullmatch(r"[a-z0-9-]{1,40}", name) is None:
        raise SystemExit(f"매니페스트 검증 실패 ({path}): service.name 문법 오류")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?){1,8}", label) is None:
        raise SystemExit(f"매니페스트 검증 실패 ({path}): service.label 문법 오류")
    if re.fullmatch(r"[a-z0-9-]{1,40}", group) is None:
        raise SystemExit(f"매니페스트 검증 실패 ({path}): service.group 문법 오류")
PY
}

audit_svcctl || die "macosctl-helper 감사에 실패했다"

install_migrated_namespace() {
    local migration_root config_root old_policy service_user sudoers_tmp
    migration_root="${STAGING_ROOT:-/}"
    config_root="$(target "/etc/macosctl")"
    local migration_args=(--root "$migration_root")
    local retire_args=(
        --root "$migration_root"
        --expected-cli-target "$EXPECTED_OLD_CLI_TARGET"
    )
    if [[ "$migration_root" == "/" ]]; then
        migration_args+=(--live)
        retire_args+=(--live)
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
        migration_args+=(--dry-run)
    fi

    MACOSCTL_INSTALLER_MIGRATION=1 \
        python3 "${REPO}/tools/migrate-namespace.py" "${migration_args[@]}" \
        || die "구 svc namespace를 안전하게 이식하지 못했다"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        old_policy="$(target "/etc/svc/policy.json")"
        service_user="$(python3 - "$old_policy" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["service_user"])
PY
)" || die "구 policy에서 service_user를 읽을 수 없다"
        retire_args+=(--service-user "$service_user" --dry-run)
        MACOSCTL_INSTALLER_MIGRATION=1 \
            python3 "${REPO}/tools/retire-namespace.py" "${retire_args[@]}" \
            >/dev/null \
            || die "구 svc integration을 안전하게 retirement할 수 없다"
        log "dry-run ③ ${SVCCTL_PATH} + ${SUDOERS_PATH}"
        log "dry-run ④ ${CLI_PATH} -> ${CLI_SOURCE}"
        log "dry-run ⑤ doctor + status --json + apply --dry-run"
        log "dry-run ⑥ 검증된 구 svc integration/config/state 제거"
        return 0
    fi

    if [[ -n "$STAGING_ROOT" ]]; then
        ROOT_INSTALL=(install)
    else
        ROOT_INSTALL=(install -o root -g wheel)
    fi
    service_user="$(python3 - "${config_root}/policy.json" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["service_user"])
PY
)" || die "새 policy에서 service_user를 읽을 수 없다"

    "${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$SVCCTL_PATH")"
    "${ROOT_INSTALL[@]}" -m 0755 "$SVCCTL_AUDIT_PATH" "$SVCCTL_PATH"
    "${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$SUDOERS_PATH")"
    sudoers_tmp="${SUDOERS_PATH}.tmp.$$"
    printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/macosctl-helper\n' \
        "$service_user" > "$sudoers_tmp"
    chmod 0440 "$sudoers_tmp"
    if [[ -z "$STAGING_ROOT" ]]; then
        chown root:wheel "$sudoers_tmp"
        /usr/sbin/visudo -cf "$sudoers_tmp" >/dev/null \
            || { rm -f "$sudoers_tmp"; die "sudoers 문법 검증 실패"; }
    fi
    mv -f "$sudoers_tmp" "$SUDOERS_PATH"
    log "③ 설치 ${SVCCTL_PATH} + ${SUDOERS_PATH}"

    "${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$CLI_PATH")"
    if [[ -e "$CLI_PATH" && ! -L "$CLI_PATH" ]]; then
        die "CLI 경로에 일반 파일이 있어 덮어쓰지 않는다: $CLI_PATH"
    fi
    ln -sfn "$CLI_SOURCE" "$CLI_PATH"
    log "④ 링크 ${CLI_PATH} -> ${CLI_SOURCE}"

    if [[ -n "$STAGING_ROOT" ]]; then
        "$CLI_SOURCE" --config-root "$config_root" status --json >/dev/null \
            || die "새 namespace status 게이트 실패"
        "$CLI_SOURCE" --config-root "$config_root" manifest --json >/dev/null \
            || die "새 namespace manifest 게이트 실패"
        "$CLI_SOURCE" --config-root "$config_root" apply --dry-run \
            --inventory "$(target "/var/db/macosctl/inventory")" >/dev/null \
            || die "새 namespace apply --dry-run 게이트 실패"
    else
        "$CLI_PATH" doctor >/dev/null || die "새 namespace doctor 게이트 실패"
        "$CLI_PATH" status --json >/dev/null || die "새 namespace status 게이트 실패"
        "$CLI_PATH" apply --dry-run >/dev/null \
            || die "새 namespace apply --dry-run 게이트 실패"
    fi
    log "⑤ 새 namespace 검증 완료"

    local recheck_args=(--root "$migration_root" --dry-run)
    if [[ "$migration_root" == "/" ]]; then
        recheck_args+=(--live)
    fi
    MACOSCTL_INSTALLER_MIGRATION=1 \
        python3 "${REPO}/tools/migrate-namespace.py" "${recheck_args[@]}" \
        >/dev/null \
        || die "구/new namespace 동일성 재검증에 실패했다"

    retire_args+=(--service-user "$service_user")
    if [[ -n "$STAGING_ROOT" ]]; then
        retire_args+=(--dry-run)
    fi
    MACOSCTL_INSTALLER_MIGRATION=1 \
        python3 "${REPO}/tools/retire-namespace.py" "${retire_args[@]}" \
        >/dev/null \
        || die "구 svc integration/config/state retirement에 실패했다"

    if [[ -n "$STAGING_ROOT" ]]; then
        log "staging에서는 구 svc namespace를 보존했다"
        return 0
    fi
    log "⑥ 검증된 구 svc integration/config/state 제거 완료"
}

if [[ "$MIGRATE_NAMESPACE" -eq 1 ]]; then
    install_migrated_namespace
    exit 0
fi

validate_manifest || die "매니페스트 사전 검증에 실패했다"

# Every deterministic collision is resolved before writing policy.json. macosctl.toml
# is human-owned after migration, so only a byte-identical partial-install output
# may coexist with a still-unsplit legacy source.
python3 - "$MANIFEST_PATH" "$DEFAULTS_PATH" <<'PY' \
    || die "기존 macosctl.toml이 달라 설치하지 않는다: $DEFAULTS_PATH"
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
data = tomllib.loads(source.read_text())
defaults = data.get("defaults")
if defaults is None or not os.path.lexists(target):
    raise SystemExit(0)
info = target.lstat()
if not stat.S_ISREG(info.st_mode):
    raise SystemExit(1)

def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise SystemExit(1)

lines = ["schema = 1", "", "[defaults]"]
for key, value in defaults.items():
    if key != "user":
        lines.append(f"{key} = {toml_value(value)}")
expected = ("\n".join(lines) + "\n").encode()
raise SystemExit(0 if target.read_bytes() == expected else 1)
PY

for expected_dir in "${POLICY_PATH%/policy.json}" "$CONF_PATH"; do
    if [[ -L "$expected_dir" || ( -e "$expected_dir" && ! -d "$expected_dir" ) ]]; then
        die "설치 디렉터리 경로 형식이 잘못됐다: $expected_dir"
    fi
done
for expected_file in "$POLICY_PATH" "$SVCCTL_PATH" "$SUDOERS_PATH"; do
    if [[ -L "$expected_file" || ( -e "$expected_file" && ! -f "$expected_file" ) ]]; then
        die "설치 파일 경로 형식이 잘못됐다: $expected_file"
    fi
done

MANIFEST_TARGET="$(python3 - "$MANIFEST_PATH" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve(strict=True))
PY
)"

if [[ -L "$FRAGMENT_PATH" ]]; then
    [[ "$(readlink "$FRAGMENT_PATH")" == "$MANIFEST_TARGET" ]] \
        || die "다른 fragment 링크가 이미 있다: $FRAGMENT_PATH"
elif [[ -e "$FRAGMENT_PATH" ]]; then
    die "fragment 경로에 일반 파일이 있다: $FRAGMENT_PATH"
fi

# Only our installed macosctl link may be switched during an ordinary install.
# Legacy svc authority is explicit and confined to --migrate-namespace.
if [[ -L "$CLI_PATH" ]]; then
    EXISTING_CLI_TARGET="$(readlink "$CLI_PATH")"
    [[ "$EXISTING_CLI_TARGET" == "$CLI_SOURCE" ]] \
        || die "다른 CLI 링크가 있어 설치하지 않는다: $CLI_PATH"
elif [[ -e "$CLI_PATH" ]]; then
    die "CLI 경로에 일반 파일이 있어 설치하지 않는다: $CLI_PATH"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run ① ${POLICY_PATH} (root:wheel 0644; 상위 /etc/macosctl 0755)"
    log "dry-run ② ${DEFAULTS_PATH} 생성 + ${MANIFEST_PATH}에서 defaults/user 분리"
    log "dry-run ③ ${FRAGMENT_PATH} -> ${MANIFEST_PATH} (conf.d 사용자 소유 0755)"
    log "dry-run ④ ${SVCCTL_PATH} (root:wheel 0755) + ${SUDOERS_PATH} (0440)"
    log "dry-run ⑤ ${CLI_PATH} -> ${CLI_SOURCE}"
    exit 0
fi

if [[ -n "$STAGING_ROOT" ]]; then
    ROOT_INSTALL=(install)
    SERVICE_UID="$(id -u)"
    SERVICE_GID="$(id -g)"
else
    ROOT_INSTALL=(install -o root -g wheel)
    SERVICE_USER="$(python3 - "$MANIFEST_PATH" "$POLICY_PATH" <<'PY'
import json, sys, tomllib
from pathlib import Path
data = tomllib.loads(Path(sys.argv[1]).read_text())
if "defaults" in data:
    print(data["defaults"]["user"])
else:
    print(json.loads(Path(sys.argv[2]).read_text())["service_user"])
PY
)"
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")" \
        || die "서비스 사용자 그룹을 찾을 수 없다: $SERVICE_USER"
    SERVICE_UID="$(id -u "$SERVICE_USER")"
    SERVICE_GID="$(id -g "$SERVICE_USER")"
fi

# ① Root-owned trust anchor.  This must complete before svcctl replacement.
"${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$POLICY_PATH")"
python3 - "$MANIFEST_PATH" "$POLICY_PATH" <<'PY'
import json
import os
import tempfile
import tomllib
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
data = tomllib.loads(source.read_text())
defaults = data.get("defaults")
if defaults is None:
    # A previous attempt got through the split.  The root-owned policy is
    # already the trust anchor and was validated by the read-only preflight.
    raise SystemExit(0)
prefix = "com.korellas."
exceptions = {}
groups = []
for service in data["service"]:
    name = service["name"]
    label = service["label"]
    if label != prefix + name:
        exceptions[name] = label
    group = service["group"]
    if group not in groups:
        groups.append(group)
policy = {
    "schema": 1,
    "label_prefix": prefix,
    "label_exceptions": exceptions,
    "service_user": defaults["user"],
    "groups": groups,
}
payload = (json.dumps(policy, ensure_ascii=False, indent=2) + "\n").encode()
fd, temporary = tempfile.mkstemp(prefix=".policy.", dir=target.parent)
try:
    os.fchmod(fd, 0o644)
    os.write(fd, payload)
    os.fsync(fd)
    os.close(fd)
    fd = -1
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if fd >= 0:
        os.close(fd)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
chmod 0644 "$POLICY_PATH"
if [[ -z "$STAGING_ROOT" ]]; then chown root:wheel "$POLICY_PATH"; fi
log "① 설치 ${POLICY_PATH} (root:wheel 0644)"

# ② Move [defaults] into macosctl.toml and remove the legacy user key. Resolve a
# repository symlink so the source fragment itself, not the symlink inode, is rewritten.
python3 - "$MANIFEST_PATH" "$DEFAULTS_PATH" <<'PY'
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

source_arg = Path(sys.argv[1])
source = source_arg.resolve(strict=True)
defaults_target = Path(sys.argv[2])
raw = source.read_text()
data = tomllib.loads(raw)
defaults = data.get("defaults")
if defaults is None:
    # Already split: preflight validated the existing macosctl.toml and policy.
    raise SystemExit(0)

def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise SystemExit(f"macosctl.toml로 옮길 수 없는 defaults 값 형식: {type(value).__name__}")

defaults_lines = ["schema = 1", "", "[defaults]"]
for key, value in defaults.items():
    if key != "user":
        defaults_lines.append(f"{key} = {toml_value(value)}")
defaults_payload = ("\n".join(defaults_lines) + "\n").encode()

# Remove the defaults header and assignments, retaining comments so human context
# is not silently discarded.  All legacy defaults are single-line scalar values.
fragment_lines = []
in_defaults = False
for line in raw.splitlines(keepends=True):
    stripped = line.strip()
    if re.fullmatch(r"\[defaults\](?:\s*#.*)?", stripped):
        in_defaults = True
        continue
    if in_defaults and stripped.startswith("["):
        in_defaults = False
    if in_defaults and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", stripped):
        continue
    fragment_lines.append(line)
fragment = "".join(fragment_lines)
if "schema" not in data:
    fragment = "schema = 1\n" + fragment
fragment_payload = fragment.encode()

def replace_atomic(path, payload, mode, *, preserve=None):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if preserve is not None:
            os.fchown(fd, preserve.st_uid, preserve.st_gid)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

replace_atomic(defaults_target, defaults_payload, 0o644)
source_stat = source.stat()
replace_atomic(
    source,
    fragment_payload,
    stat.S_IMODE(source_stat.st_mode),
    preserve=source_stat,
)
PY
chmod 0644 "$DEFAULTS_PATH"
if [[ -z "$STAGING_ROOT" ]]; then
    chown "$SERVICE_USER:$SERVICE_GROUP" "$DEFAULTS_PATH"
fi
log "② 분리 ${DEFAULTS_PATH}; ${MANIFEST_PATH}에서 defaults/user 제거"

# ③ User-owned fragment registration.  Refuse a foreign file instead of clobbering it.
python3 - "${POLICY_PATH%/policy.json}" "$MANIFEST_TARGET" \
    "$SERVICE_UID" "$SERVICE_GID" <<'PY' \
    || die "conf.d fragment를 안전하게 게시하지 못했다"
import os
import stat
import sys
from pathlib import Path

config = Path(sys.argv[1])
manifest_target = sys.argv[2]
service_uid = int(sys.argv[3])
service_gid = int(sys.argv[4])
fragment_name = "30-ai.toml"
directory_flags = os.O_RDONLY | os.O_NOFOLLOW
if hasattr(os, "O_DIRECTORY"):
    directory_flags |= os.O_DIRECTORY

def same_identity(left, right):
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino

config_fd = -1
conf_fd = -1
try:
    config_fd = os.open(config, directory_flags)
    try:
        conf_fd = os.open("conf.d", directory_flags, dir_fd=config_fd)
    except FileNotFoundError:
        os.mkdir("conf.d", 0o755, dir_fd=config_fd)
        conf_fd = os.open("conf.d", directory_flags, dir_fd=config_fd)

    conf_stat = os.fstat(conf_fd)
    if not stat.S_ISDIR(conf_stat.st_mode):
        raise OSError("conf.d fd가 디렉터리가 아니다")
    parent_stat = os.stat("conf.d", dir_fd=config_fd, follow_symlinks=False)
    if not stat.S_ISDIR(parent_stat.st_mode) or not same_identity(conf_stat, parent_stat):
        raise OSError("conf.d parent entry가 열린 fd와 다르다")

    os.fchmod(conf_fd, 0o755)
    os.fchown(conf_fd, service_uid, service_gid)
    try:
        fragment_stat = os.stat(
            fragment_name,
            dir_fd=conf_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        os.symlink(manifest_target, fragment_name, dir_fd=conf_fd)
    else:
        if not stat.S_ISLNK(fragment_stat.st_mode):
            raise OSError("30-ai.toml이 installer 소유 심링크가 아니다")
        if os.readlink(fragment_name, dir_fd=conf_fd) != manifest_target:
            raise OSError("30-ai.toml이 다른 대상을 가리킨다")

    os.chown(
        fragment_name,
        service_uid,
        service_gid,
        dir_fd=conf_fd,
        follow_symlinks=False,
    )
    os.fsync(conf_fd)

    final_parent_stat = os.stat(
        "conf.d",
        dir_fd=config_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(final_parent_stat.st_mode)
        or not same_identity(conf_stat, final_parent_stat)
    ):
        raise OSError("게시 중 conf.d parent entry가 교체됐다")
except OSError as exc:
    raise SystemExit(f"conf.d 안전 게시 실패: {exc}")
finally:
    if conf_fd >= 0:
        os.close(conf_fd)
    if config_fd >= 0:
        os.close(config_fd)
PY
log "③ 링크 ${FRAGMENT_PATH} -> ${MANIFEST_TARGET}"

# ④ Replace the self-contained root wrapper, then its wildcard-free sudo rule.
"${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$SVCCTL_PATH")"
"${ROOT_INSTALL[@]}" -d -m 0755 "$(target "/var/db/macosctl")"
"${ROOT_INSTALL[@]}" -m 0755 "$SVCCTL_AUDIT_PATH" "$SVCCTL_PATH"
"${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$SUDOERS_PATH")"
SERVICE_USER_FOR_RULE="$(python3 - "$POLICY_PATH" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["service_user"])
PY
)"
SUDOERS_TMP="${SUDOERS_PATH}.tmp.$$"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/macosctl-helper\n' \
    "$SERVICE_USER_FOR_RULE" > "$SUDOERS_TMP"
chmod 0440 "$SUDOERS_TMP"
if [[ -z "$STAGING_ROOT" ]]; then
    chown root:wheel "$SUDOERS_TMP"
    /usr/sbin/visudo -cf "$SUDOERS_TMP" >/dev/null \
        || { rm -f "$SUDOERS_TMP"; die "sudoers 문법 검증 실패"; }
fi
mv -f "$SUDOERS_TMP" "$SUDOERS_PATH"
chmod 0440 "$SUDOERS_PATH"
log "④ 설치 ${SVCCTL_PATH} (0755) + ${SUDOERS_PATH} (0440)"

# ⑤ Switch only after policy/config/root control are all usable.
"${ROOT_INSTALL[@]}" -d -m 0755 "$(dirname "$CLI_PATH")"
if [[ -e "$CLI_PATH" && ! -L "$CLI_PATH" ]]; then
    die "CLI 경로에 일반 파일이 있어 덮어쓰지 않는다: $CLI_PATH"
fi
ln -sfn "$CLI_SOURCE" "$CLI_PATH"
chmod -h 0755 "$CLI_PATH" 2>/dev/null || true
log "⑤ 링크 ${CLI_PATH} -> ${CLI_SOURCE}"
log "완료. 서비스 변경은 하지 않았다; 컷오버 게이트 뒤 sudo macosctl apply를 실행할 것."
