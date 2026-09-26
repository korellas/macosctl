"""서비스 등록 스캐폴더 — 새 서비스를 두 줄로 올린다.

설계 근거: docs/design.md
"""

from __future__ import annotations

import re
from pathlib import Path

from macosctl import policy as policy_module
from macosctl.manifest import Service

REPO = Path(__file__).resolve().parent.parent
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")

# mlx 서비스는 런처 하나를 env로 구분해 공유한다. 새 인스턴스마다 스크립트를
# 복제하면 수정이 갈라진다.
SHARED_LAUNCHERS = {"mlx": "run-mlx-lm.sh"}

GROUPS = ("infra", "mtplx", "mlx", "llm", "edge", "dashboard")


def check_conflicts(
    name: str,
    port: int,
    services: tuple[Service, ...],
    *,
    group: str | None = None,
    label_policy: policy_module.Policy | None = None,
) -> tuple[str, ...]:
    """등록 전에 걸릴 것들을 미리 말해준다."""
    problems = []

    if not NAME_PATTERN.match(name):
        # macosctl-helper의 규칙과 같아야 한다 — 통과하지 못할 이름을 만들어주면,
        # 등록은 되는데 제어가 안 되는 서비스가 생긴다.
        problems.append(
            f"이름 '{name}'이 규칙에 안 맞는다 (소문자·숫자·하이픈, 1~40자)"
        )
    if any(s.name == name for s in services):
        problems.append(f"이름 '{name}'이 이미 매니페스트에 있다")
    if any(s.port == port for s in services):
        owner = next(s.name for s in services if s.port == port)
        problems.append(f"포트 {port}은 이미 {owner}가 쓴다")
    if label_policy is not None:
        if policy_module.resolve_label(name, label_policy) is None:
            problems.append(f"이름 '{name}'으로 유효한 policy 라벨을 만들 수 없다")
        if group is None or group not in label_policy.groups:
            problems.append(
                f"그룹 '{group}'이 policy groups에 없다 "
                f"({', '.join(label_policy.groups)})"
            )

    return tuple(problems)


def render_block(
    name: str,
    port: int,
    group: str,
    model: str | None = None,
    mem_budget: str | None = None,
    launcher_dir: Path = REPO,
    label: str | None = None,
) -> str:
    """`[[service]]` 블록 하나를 만든다."""
    resolved_label = label or f"com.korellas.{name}"
    launcher = SHARED_LAUNCHERS.get(group)
    if launcher:
        exec_argv = [f'"{launcher_dir}/{launcher}"', '"foreground"']
        env_pairs = [f'MODEL = "{model or "org/model-id"}"',
                     f'PORT = "{port}"',
                     f'SESSION = "{name}"']
    else:
        exec_argv = [f'"{launcher_dir}/run-{name}.sh"', '"foreground"']
        env_pairs = []

    lines = [
        "",
        "[[service]]",
        f'name        = "{name}"',
        f'label       = "{resolved_label}"',
        f"port        = {port}",
        f'group       = "{group}"',
    ]
    if mem_budget:
        lines.append(f'mem_budget  = "{mem_budget}"')
    if env_pairs:
        lines.append("env         = { " + ", ".join(env_pairs) + " }")
    lines.append("exec        = [" + ", ".join(exec_argv) + "]")
    lines.append("depends_on  = []")
    return "\n".join(lines) + "\n"


def append_block(path: Path, block: str) -> None:
    """매니페스트 끝에 블록을 붙인다."""
    existing = path.read_text()
    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(existing + separator + block)


def launcher_hint(name: str, group: str, launcher_dir: Path = REPO) -> str:
    """다음에 사람이 해야 할 일."""
    launcher = SHARED_LAUNCHERS.get(group)
    if launcher:
        return (
            f"런처는 기존 {launcher}를 그대로 쓴다 (MODEL/PORT/SESSION만 다르다).\n"
            f"  새 스크립트를 만들지 말 것 — 복제하면 수정이 갈라진다."
        )
    return (
        f"런처 {launcher_dir}/run-{name}.sh를 만들어야 한다.\n"
        f"  foreground 서브커맨드가 필수다 — 그게 없으면 launchd가 즉시 종료를\n"
        f"  '죽었다'로 읽고 KeepAlive가 무한 재기동한다.\n"
        f"  가장 가까운 예: run-mlx-lm.sh (짧다), run-mtplx.sh (부트 락까지)."
    )
