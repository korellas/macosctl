"""reconcile 엔진 — 매니페스트를 시스템에 수렴시킨다.

설계 근거: docs/design.md

계획(`build_plan`)과 실행(`execute`)을 분리한다. 계획은 순수 함수라 root 없이
테스트할 수 있고, `--dry-run`이 보여주는 것이 정확히 실행될 것과 같아진다.

변경분만 반영해 불필요한 서비스 재기동을 피한다.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from macosctl import collect
from macosctl import confd
from macosctl import inventory
from macosctl import plist as plistgen
from macosctl import state
from macosctl.manifest import Defaults, Service
from macosctl.model import MergedModel

DAEMON_DIR = Path("/Library/LaunchDaemons")
APPLY_LOCK = Path("/var/db/macosctl/apply.lock")
LOG_ROTATE_LABEL = "com.korellas.log-rotate"
BOOTOUT_TIMEOUT_SECONDS = 30
# 등록 직후 launchd가 PID를 붙이기까지의 상한. 포트 readiness가 아니라 **PID**만
# 기다린다 — 장기 준비는 `macosctl start/restart --wait`의 몫이다.
ACTIVATION_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Unit:
    """설치 대상 하나 — 서비스이거나 log-rotate 같은 인프라 잡이다."""
    label: str
    name: str
    body: bytes
    source: str | None = None


@dataclass(frozen=True)
class Plan:
    create: tuple[Unit, ...] = ()
    changed: tuple[Unit, ...] = ()
    unchanged: tuple[Unit, ...] = ()
    retire: tuple[str, ...] = ()
    mask: tuple[str, ...] = ()
    inventory_missing: bool = False
    mask_sources: tuple[tuple[str, str | None], ...] = ()

    @property
    def touched(self) -> tuple[Unit, ...]:
        return self.create + self.changed

    def is_noop(self) -> bool:
        return not (self.create or self.changed or self.retire or self.mask)


class ServiceSelectionError(ValueError):
    pass


def select_services(
    plan: Plan,
    services: tuple[Service, ...],
    names: tuple[str, ...],
) -> Plan:
    """Restrict an already validated full plan to explicitly named services.

    Selection happens after full model/policy validation.  Infrastructure units
    and unrelated create/change/retire/mask actions are excluded, while the
    executor keeps the untouched inventory entries it received.
    """
    requested = tuple(dict.fromkeys(names))
    if not requested:
        return plan

    by_name = {service.name: service for service in services}
    unknown = tuple(name for name in requested if name not in by_name)
    if unknown:
        raise ServiceSelectionError(
            f"선언되지 않은 서비스: {', '.join(unknown)}"
        )
    unmanaged = tuple(name for name in requested if not by_name[name].managed)
    if unmanaged:
        raise ServiceSelectionError(
            f"managed가 아닌 서비스는 apply할 수 없다: {', '.join(unmanaged)}"
        )

    labels = {by_name[name].label for name in requested}
    selected_units = lambda units: tuple(  # noqa: E731
        unit for unit in units if unit.label in labels
    )
    return Plan(
        create=selected_units(plan.create),
        changed=selected_units(plan.changed),
        unchanged=selected_units(plan.unchanged),
        retire=tuple(label for label in plan.retire if label in labels),
        mask=tuple(label for label in plan.mask if label in labels),
        mask_sources=tuple(
            item for item in plan.mask_sources if item[0] in labels
        ),
        inventory_missing=plan.inventory_missing,
    )


def desired_units(
    services: tuple[Service, ...], defaults: Defaults, repo: Path, config_root: Path
) -> tuple[Unit, ...]:
    """설치해야 할 것 전체 = managed 서비스 + 인프라 잡(log-rotate).

    log-rotate를 여기 넣는 것이 D4의 요점이다. 특례 코드로 빼두면 접두사 기반
    은퇴가 그것을 죽인다.
    """
    units = [
        Unit(
            label=s.label,
            name=s.name,
            body=plistgen.build(s, defaults),
            source=s.source_of("name") if hasattr(s, "source_of") else None,
        )
        for s in services
        if s.managed
    ]
    units.append(
        Unit(
            label=LOG_ROTATE_LABEL,
            name="log-rotate",
            body=plistgen.build_log_rotate(
                defaults, repo, Path(config_root) / confd.DEFAULTS_BASENAME
            ),
        )
    )
    return tuple(units)


def build_plan(
    merged: MergedModel,
    defaults: Defaults,
    repo: Path,
    known_inventory: dict[str, inventory.Entry] | None,
    daemon_dir: Path = DAEMON_DIR,
    *,
    config_root: Path,
) -> Plan:
    """무엇을 만들고/바꾸고/은퇴시킬지 정한다. 부작용 없음."""
    services = merged.services
    units = desired_units(services, defaults, repo, config_root)
    desired_labels = {u.label for u in units}
    masked_labels = {
        service.label for service in services if service.lifecycle == "masked"
    }
    masked_sources = {
        service.label: service.source_of("name")
        for service in services if service.lifecycle == "masked"
    }

    create, changed, unchanged = [], [], []
    for unit in units:
        target = daemon_dir / f"{unit.label}.plist"
        if not target.exists():
            create.append(unit)
        elif (
            plistgen.same_as_installed(unit.body, target)
            and (
                known_inventory is None
                or unit.label not in known_inventory
                or known_inventory[unit.label].lifecycle == "active"
            )
        ):
            unchanged.append(unit)
        else:
            changed.append(unit)

    # 은퇴 대상은 **인벤토리 안에서만** 찾는다. 인벤토리가 없으면 아무것도 은퇴시키지
    # 않는다 — 접두사로 판정하면 우리가 설치하지 않은 것을 죽인다 (2R 공통 CRITICAL).
    if known_inventory is None:
        retire: tuple[str, ...] = ()
        mask: tuple[str, ...] = ()
    else:
        known_labels = set(known_inventory)
        retire = tuple(sorted(known_labels - desired_labels - masked_labels))
        mask = tuple(sorted(
            label for label in known_labels & masked_labels
            if known_inventory[label].lifecycle != "masked"
            or known_inventory[label].source != masked_sources[label]
            or (daemon_dir / f"{label}.plist").exists()
        ))

    return Plan(
        create=tuple(create),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        retire=retire,
        mask=mask,
        mask_sources=tuple((label, masked_sources[label]) for label in mask),
        inventory_missing=known_inventory is None,
    )


# --- 실행 ------------------------------------------------------------------------


@dataclass
class Result:
    installed: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    masked: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def exit_code(self) -> int:
        """부분 성공은 명시적 비정상 종료다 (2R Codex #3).

        'A 성공 / B 실패 / C 미처리'가 단일 성공으로 보고되면 개별 서비스의
        실제 상태를 나중에 복구할 수 없다.
        """
        return 1 if self.failed else 0


class ApplyRefused(Exception):
    """변경 **전에** 관측이 불충분했다 — 아무것도 바꾸지 않았다."""


@dataclass(frozen=True)
class Observation:
    """변경 직전에 관측한 유닛의 사실. 세 축 모두 launchd/state의 정본에서 온다."""

    was_active: bool
    was_boot_disabled: bool
    was_stopped_this_boot: bool


def activate_after(observation: Observation) -> bool:
    """변경 후에 이 유닛을 살려 둘 것인가.

        activate_after = was_active or (not was_boot_disabled and not was_stopped_this_boot)

    apply는 plist를 바꿀 뿐 정책을 새로 정하지 않는다. 그래서 판정의 전부가
    "바꾸기 전에 어떤 상태였나"다:

      · active면 boot policy와 무관하게 다시 active로 돌려놓는다 (active+disabled 유지).
      · 이번 부팅에서 운영자가 정지시켰으면 존중한다 — plist만 갈아끼운다.
        생성 plist는 RunAtLoad+KeepAlive라 **bootstrap 하는 순간** 프로세스가 뜬다.
      · boot-disabled인 inactive 서비스를 apply가 깨우면 그것이 곧 정책 변경이다.
    """
    return observation.was_active or not (
        observation.was_boot_disabled or observation.was_stopped_this_boot
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True,
        timeout=60, check=False,
    )


# `launchctl print`의 반환코드:
#   0   = 존재
#   113 = 그런 서비스가 없음
#   64  = 사용법/타깃 오류 등 그 밖
JOB_ABSENT_RC = 113


def _job_exists(label: str) -> bool:
    """잡이 도메인에 있는가.

    **모든 non-zero를 '없음'으로 읽으면 안 된다** (2R Codex). 일시적 오류가
    bootout 성공으로 오판되면 살아 있는 프로세스를 은퇴시킨다. 확실히 '없다'고
    말하는 113만 부재로 보고, 나머지는 fail-closed로 '있다'고 간주한다.
    """
    rc = _launchctl("print", f"system/{label}").returncode
    if rc == 0:
        return True
    if rc == JOB_ABSENT_RC:
        return False
    return True


def _job_pid(label: str) -> int | None:
    """실제 runtime 정본은 launchd의 PID다 — state 파일이 아니다."""
    result = _launchctl("print", f"system/{label}")
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            value = stripped[len("pid = "):].strip()
            return int(value) if value.isdigit() else None
    return None


def _wait_for_pid(label: str) -> int | None:
    """새 PID가 보일 때까지 기다린다 — 성공의 정본은 rc가 아니라 이 관측이다.

    잡이 도메인에 **확실히 없으면**(113) 기다릴 이유가 없다. 등록 자체가 되지
    않은 것이므로 즉시 실패로 돌린다. 그 밖의 관측 오류는 `_job_exists`가
    fail-closed로 '있다'고 보므로 상한까지 폴링한다.
    """
    deadline = time.monotonic() + ACTIVATION_TIMEOUT_SECONDS
    while True:
        pid = _job_pid(label)
        if pid is not None:
            return pid
        if not _job_exists(label):
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _bootout_and_wait(label: str) -> bool:
    """bootout은 비동기다 — 도메인에서 사라질 때까지 기다린다.

    """
    if not _job_exists(label):
        return True
    _launchctl("bootout", f"system/{label}")
    deadline = time.monotonic() + BOOTOUT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _job_exists(label):
            return True
        time.sleep(0.5)
    return False


def _boot_session_uuid() -> str | None:
    """현재 부팅의 식별자. 테스트가 여기만 가로채면 된다."""
    return state.boot_session_uuid()


def _disabled_override(label: str) -> bool | None:
    """Return launchd's override, or None when it cannot be observed."""
    try:
        result = _launchctl("print-disabled", "system")
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return collect.parse_disabled_overrides(result.stdout or "").get(label, False)


def _write_plist_atomic(target: Path, body: bytes) -> None:
    """임시파일 → lint → atomic rename. 잘린 plist가 남는 경우를 없앤다."""
    problem = plistgen.lint(body)
    if problem:
        raise ValueError(f"plist가 유효하지 않다: {problem}")

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        # 운영 경로는 항상 root다 (CLI가 geteuid를 강제한다). 비-root 실행은
        # 테스트뿐이며, 거기서 chown은 불가능하고 필요하지도 않다.
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def writer_locked(lock_path: Path = APPLY_LOCK):
    """Acquire the process-wide apply writer lock."""
    with state.locked(Path(lock_path)):
        yield


def _same_lock(left: Path, right: Path) -> bool:
    left, right = Path(left), Path(right)
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def execute(
    plan: Plan,
    inventory_path: Path = inventory.DEFAULT_PATH,
    daemon_dir: Path = DAEMON_DIR,
    state_path: Path = state.DEFAULT_PATH,
    known_inventory: dict[str, inventory.Entry] | None = None,
    lock_path: Path = state.DEFAULT_LOCK,
    apply_lock_path: Path = APPLY_LOCK,
    log=print,
) -> Result:
    """계획을 적용한다. root 필요.

    인벤토리는 **각 plist rename 직후** 갱신하고, unchanged 항목도 매번 포함해
    전체를 다시 쓴다 — 그래야 기록 전 크래시 후 재실행이 수렴한다 (D4 갱신 규약).

    시작점은 **기존 인벤토리의 복사본**이다. unchanged만으로
    시작하면 변경분의 plist 쓰기가 실패했을 때 구 plist는 설치된 채인데 인벤토리에서
    빠져, macosctl-helper 인가가 막히고 이후 안전한 은퇴도 불가능한 고아가 된다.
    """
    if _same_lock(apply_lock_path, lock_path):
        raise ValueError("apply writer 락과 서비스 락은 서로 달라야 한다")
    with writer_locked(apply_lock_path):
        # D7-3: migration is a standalone operation before even a no-op apply.
        # Call the unlocked core because this writer lock is already held.
        inventory._migrate_unlocked(inventory_path)
        on_disk = inventory.read(inventory_path)
        if on_disk is not None:
            known_inventory = on_disk
        return _execute_unlocked(
            plan,
            inventory_path=inventory_path,
            daemon_dir=daemon_dir,
            state_path=state_path,
            known_inventory=known_inventory,
            lock_path=lock_path,
            log=log,
        )


def _execute_unlocked(
    plan: Plan,
    inventory_path: Path,
    daemon_dir: Path,
    state_path: Path,
    known_inventory: dict[str, inventory.Entry] | None,
    lock_path: Path,
    log,
) -> Result:
    """Execute while the caller owns ``APPLY_LOCK``."""
    result = Result()

    if known_inventory is None:
        # 호출자가 안 넘겼다고 기존 기록을 버리면 안 된다 — 안전한 쪽이 기본값이어야
        # 한다. 손상·읽기 실패를 빈 것으로 낮추면 execute가 전체를 다시 쓰면서 기존
        # 기록을 통째로 날린다. 예외를 그대로 올린다 (2R Codex, fail-closed).
        known_inventory = inventory.read(inventory_path) or {}

    # 변경 **전에** fail-closed 한다. boot policy나 stop 의도를 관측할 수 없는데
    # 진행하면 apply가 정책을 모른 채 정책을 바꿔 버린다 — 그 상태는 나중에
    # 되돌릴 근거조차 남지 않는다. 위 inventory.read는 읽기 전용이므로 이 순서에서도
    # "첫 변경 전"이라는 성질은 그대로다.
    boot_uuid, desired = _preflight(plan, state_path, known_inventory)

    # 멤버십 기준은 "plist가 설치되어 있음"이다 (bootstrap 성공 여부와 무관).
    members = _refresh_unchanged_inventory_unlocked(
        plan, inventory_path, known_inventory
    )

    def persist() -> None:
        inventory.write(inventory_path, members)

    def _read_desired() -> state.State:
        try:
            return state.read(state_path, boot_uuid=boot_uuid)
        except state.StateCorrupt as exc:
            raise ApplyRefused(f"state를 읽을 수 없다: {exc}") from exc

    # 락은 **서비스 단위**로 잡았다 놓는다. execute 전체를 감싸면 서비스당 최대
    # BOOTOUT_TIMEOUT_SECONDS의 대기 동안 macosctl-helper이 블록돼 `macosctl stop`이 분 단위로
    # 멎는다 (2R Fable). apply가 락을 아예 안 잡던 것이 D6 미이행이었다 (2R 양쪽).
    for unit in plan.touched:
        target = daemon_dir / f"{unit.label}.plist"
        try:
            with state.locked(lock_path):
                # 관측은 bootout **전에** 해야 한다 — bootout 뒤에 보면 was_active가
                # 항상 False로 읽혀 살아 있던 서비스를 되살리지 않는다.
                # 각 서비스 직전에 다시 읽는다: 시작 시점의 스냅샷을 들고 있으면 그
                # 사이 macosctl-helper이 남긴 의도를 덮어쓴다 (2R Codex #2).
                observation = _observe(unit.label, boot_uuid, _read_desired())
                if observation is None:
                    result.failed.append(
                        (unit.name, "boot policy를 관측할 수 없다 — 이 서비스는 건드리지 않았다")
                    )
                    log(f"  보류 {unit.name}: boot policy 관측 불가")
                    continue

                # bootout이 안 끝났는데 plist를 갈면, 그 뒤에 보이는 PID가 새 등록의
                # 것인지 갈아치우지 못한 옛 등록의 것인지 구분할 수 없다. 기존 등록과
                # 기존 runtime을 그대로 두고 실패로 남긴다 — 다음 apply가 재시도한다.
                if not _bootout_and_wait(unit.label):
                    result.failed.append(
                        (unit.name, f"bootout이 {BOOTOUT_TIMEOUT_SECONDS}초 안에 끝나지 "
                                    f"않았다 — plist 교체를 중단한다 (기존 등록 유지)")
                    )
                    log(f"  보류 {unit.name}: bootout 미완료 — 다음 apply가 재시도한다")
                    continue

                _write_plist_atomic(target, unit.body)
                members[unit.label] = inventory.Entry(
                    hashlib.sha256(unit.body).hexdigest(), unit.source, "active"
                )
                persist()  # rename 직후 기록 — 고아 방지의 핵심

                if not activate_after(observation):
                    result.installed.append(unit.name)
                    log(
                        f"  갱신 {unit.name}  ({unit.label}) — "
                        f"{_inactive_reason(observation)}, 기동 생략"
                    )
                    continue

                _activate_unit(unit, target, observation)
                result.installed.append(unit.name)
                log(f"  등록 {unit.name}  ({unit.label})")

                # 실제 active가 stop 의도를 이긴다 — 재부팅 부활 등으로 남은 stale
                # intent를 정리한다. **재활성화가 끝난 뒤에** 한다: 의도 파일을 못
                # 고쳤다고 runtime을 희생하면 apply 한 번이 살아 있던 서비스를 내린다.
                # 실패는 실패대로 보고하되 서비스는 active로 남는다.
                if observation.was_active and observation.was_stopped_this_boot:
                    try:
                        state.clear_stopped(
                            state_path, unit.label, boot_uuid=boot_uuid
                        )
                    except (OSError, state.StateCorrupt) as exc:
                        result.failed.append(
                            (unit.name, f"stale 정지 의도 정리 실패 (서비스는 기동됨): {exc}")
                        )
                        log(f"  경고 {unit.name}: stale 정지 의도를 지우지 못했다 — {exc}")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            result.failed.append((unit.name, str(exc)))
            log(f"  실패 {unit.name}: {exc}")

    # | | 은퇴 | mask |
    # | bootout | O | O |
    # | `enable` 복원 | O | X |
    # | plist 삭제 | O | O |
    # | 인벤토리 | 제거 | `lifecycle="masked"` 유지 |
    # | state 마크 | `clear` | 보존 |
    mask_sources = dict(plan.mask_sources)
    for label in plan.mask:
        try:
            with state.locked(lock_path):
                if label not in members:
                    result.failed.append(
                        (label, "현재 인벤토리 비멤버 — stale mask 계획을 거부한다")
                    )
                    log(f"  mask 보류 {label}: 현재 인벤토리 비멤버")
                    continue
                target = daemon_dir / f"{label}.plist"
                previous = members[label]
                needs_shutdown = (
                    previous.lifecycle != "masked" or target.exists()
                )
                if needs_shutdown and not _bootout_and_wait(label):
                    result.failed.append(
                        (label, f"bootout이 {BOOTOUT_TIMEOUT_SECONDS}초 안에 끝나지 않았다 "
                                f"— mask를 중단한다 (제어 가능 상태를 유지)")
                    )
                    log(f"  mask 보류 {label}: bootout 미완료 — 다음 apply가 재시도한다")
                    continue
                if target.exists():
                    target.unlink()
                source = (
                    mask_sources[label]
                    if label in mask_sources
                    else previous.source
                )
                members[label] = inventory.Entry(None, source, "masked")
                persist()
                result.masked.append(label)
                log(f"  정지(mask) {label}")
        except (KeyError, OSError, ValueError) as exc:
            result.failed.append((label, str(exc)))
            log(f"  mask 실패 {label}: {exc}")

    for label in plan.retire:
        try:
            with state.locked(lock_path):
                if label not in members:
                    result.failed.append(
                        (label, "현재 인벤토리 비멤버 — stale 은퇴 계획을 거부한다")
                    )
                    log(f"  은퇴 보류 {label}: 현재 인벤토리 비멤버")
                    continue

                # 실제 boot policy를 **bootout 직전에** 다시 본다. preflight와 여기
                # 사이에 관측이 끊길 수 있고, 그때 provenance로 대신하면 아무도
                # 확인하지 않은 오버라이드를 남긴 채 plist·인벤토리를 지우게 된다.
                # state.disabled_by_macosctl은 조작 출처일 뿐 실제값의 대역이 아니다.
                was_disabled = _disabled_override(label)
                if was_disabled is None:
                    result.failed.append(
                        (label, "실제 boot policy를 관측할 수 없다 "
                                "(launchctl print-disabled) — 은퇴를 중단한다")
                    )
                    log(f"  은퇴 보류 {label}: boot policy 관측 불가")
                    continue

                # bootout이 끝나지 않았는데 plist·인벤토리·state를 지우면, 프로세스는
                # 계속 도는데 macosctl-helper이 인벤토리 부재로 stop/restart를 거부한다 —
                # 실행 중이면서 제어 불가능한 상태다. 은퇴를 실패로 남기고 다음
                # apply가 실측으로 재시도하는 편이 낫다.
                if not _bootout_and_wait(label):
                    result.failed.append(
                        (label, f"bootout이 {BOOTOUT_TIMEOUT_SECONDS}초 안에 끝나지 않았다 "
                                f"— 은퇴를 중단한다 (제어 가능 상태를 유지)")
                    )
                    log(f"  은퇴 보류 {label}: bootout 미완료 — 다음 apply가 재시도한다")
                    continue

                # launchctl에는 오버라이드 **항목**을 지우는 verb가 없다. disabled인 채로
                # 은퇴하면 같은 label을 재등록했을 때 bootstrap이 119로 영구 실패한다.
                #
                # 반환값**과** 후조건을 함께 본다 — rc 0만 믿고 지우면 그 오버라이드는
                # 영구히 남는다 (2R Codex).
                restore = _launchctl("enable", f"system/{label}")
                if restore.returncode != 0:
                    result.failed.append(
                        (label, f"disabled 오버라이드 복원 실패 "
                                f"({(restore.stderr or restore.stdout).strip()}) — 은퇴를 중단한다")
                    )
                    log(f"  은퇴 보류 {label}: enable 복원 실패 — 다음 apply가 재시도한다")
                    continue

                restored = _disabled_override(label)
                if restored is not False:
                    detail = (
                        "복원 결과를 관측할 수 없다" if restored is None
                        else "enable 뒤에도 여전히 disabled다"
                    )
                    result.failed.append(
                        (label, f"disabled 오버라이드 복원 확인 실패 ({detail}) "
                                f"— 은퇴를 중단한다")
                    )
                    log(f"  은퇴 보류 {label}: enable 후조건 미확인 — 다음 apply가 재시도한다")
                    continue
                if was_disabled:
                    log(f"  경고: {label}의 disabled 오버라이드를 복원했다")

                target = daemon_dir / f"{label}.plist"
                if target.exists():
                    target.unlink()
                # 은퇴와 함께 의도도 지운다 — 남겨두면 같은 이름을 재등록했을 때
                # 과거 의도를 상속해 '새 서비스가 안 뜨는' 미스터리가 된다.
                state.forget(state_path, label, boot_uuid=boot_uuid)
                members.pop(label, None)
                persist()
                result.retired.append(label)
                log(f"  은퇴 {label}")
        except OSError as exc:
            result.failed.append((label, str(exc)))
            log(f"  은퇴 실패 {label}: {exc}")

    persist()
    return result


def _preflight(
    plan: Plan,
    state_path: Path,
    known_inventory: dict[str, inventory.Entry],
) -> tuple[str | None, state.State]:
    """건드릴 유닛이 있으면 boot session과 state를 **변경 전에** 확보한다."""
    try:
        desired = state.read(state_path)
    except state.StateCorrupt as exc:
        raise ApplyRefused(
            f"state가 손상돼 정지 의도를 판정할 수 없다 — 아무것도 바꾸지 않았다: {exc}"
        ) from exc

    # 은퇴도 launchd를 바꾸고 state 의도를 지운다 — touched와 같은 boot scope를
    # 요구한다. 여기서 별도 소유권을 만들지 않는 것이 요점이다.
    #
    # 다만 현재 인벤토리 비멤버는 아래에서 손대지 않고 거부된다. 건드리지도 않을
    # 라벨의 관측 실패로 apply 전체를 멈추면, stale 계획 하나가 정상 은퇴까지
    # 막는다 — 그래서 실제 은퇴 대상만 preflight한다.
    labels = tuple(unit.label for unit in plan.touched) + tuple(
        label for label in plan.retire if label in known_inventory
    )
    if not labels:
        return None, desired

    boot_uuid = _boot_session_uuid()
    if boot_uuid is None:
        raise ApplyRefused(
            "현재 boot session UUID를 읽을 수 없다 — 정지 의도의 유효 범위를 "
            "판정할 수 없어 아무것도 바꾸지 않았다"
        )

    # boot policy의 정본은 launchd다. 그것을 못 읽으면 어떤 유닛을 살릴지도,
    # 어떤 오버라이드를 청소해야 하는지도 정할 근거가 없다 — 첫 bootout 전에 멈춘다.
    for label in labels:
        if _disabled_override(label) is None:
            raise ApplyRefused(
                f"{label}의 boot policy를 관측할 수 없다 "
                f"(launchctl print-disabled) — 아무것도 바꾸지 않았다"
            )
    return boot_uuid, state.read(state_path, boot_uuid=boot_uuid)


def _observe(
    label: str, boot_uuid: str | None, desired: state.State
) -> Observation | None:
    """변경 직전 사실 관측. boot policy를 못 보면 None (호출자가 건너뛴다)."""
    boot_disabled = _disabled_override(label)
    if boot_disabled is None:
        return None
    return Observation(
        was_active=_job_pid(label) is not None,
        was_boot_disabled=boot_disabled,
        was_stopped_this_boot=desired.is_stopped_this_boot(label, boot_uuid),
    )


def _inactive_reason(observation: Observation) -> str:
    if observation.was_stopped_this_boot:
        return "이번 부팅의 정지 의도 유지"
    return "boot 차단(disabled) 상태 유지"


def _activate_unit(unit: Unit, target: Path, observation: Observation) -> None:
    """등록→기동. boot-disabled였다면 임시 enable로 감싸고 반드시 되돌린다.

    enable이 bootstrap보다 **먼저**여야 한다. disabled 오버라이드가 걸린 서비스는
    bootstrap이 "Bootstrap failed: 119: Service is disabled"로 죽는다. 그리고 그
    enable은 이 서비스가 원래 boot-disabled였다면 반드시 되돌려야 한다 — 되돌리지
    않으면 apply가 boot policy를 조용히 바꾼 것이 된다.
    """
    try:
        _launchctl("enable", f"system/{unit.label}")
        boot = _launchctl("bootstrap", "system", str(target))
        # 성공 판정은 bootstrap rc가 아니라 launchd의 실제 PID다. rc가 0이어도
        # 아무것도 뜨지 않았으면 실패이고, rc가 0이 아니어도 새 PID가 관측되면
        # 후조건이 지배한다. 위에서 bootout 완료를 확인했으므로 여기서 보이는
        # PID는 반드시 이번 등록의 것이다.
        pid = _wait_for_pid(unit.label)
    finally:
        restore = _restore_boot_policy(unit.label, observation.was_boot_disabled)

    if pid is None:
        detail = (boot.stderr or boot.stdout or "").strip()
        message = f"bootstrap 후 PID를 확인할 수 없다: {detail}".rstrip(": ")
        raise RuntimeError(f"{message}; {restore}" if restore else message)
    if restore:
        raise RuntimeError(restore)


def _restore_boot_policy(label: str, was_disabled: bool) -> str | None:
    """임시 enable을 되돌리고 관측으로 확인한다. 실패는 정책 변경이므로 보고한다."""
    if not was_disabled:
        return None
    _launchctl("disable", f"system/{label}")
    if _disabled_override(label) is True:
        return None
    return (
        f"{label}의 boot 차단(disabled) 복원을 확인하지 못했다 — "
        f"`macosctl disable`로 확인할 것"
    )


def _refresh_unchanged_inventory_unlocked(
    plan: Plan,
    inventory_path: Path,
    known_inventory: dict[str, inventory.Entry],
) -> dict[str, inventory.Entry]:
    """Refresh metadata only for entries already owned by this inventory."""
    members = dict(known_inventory)
    changed = False
    for unit in plan.unchanged:
        if (
            unit.label not in members
            or members[unit.label].lifecycle != "active"
        ):
            continue
        current = inventory.Entry(
            hashlib.sha256(unit.body).hexdigest(), unit.source, "active"
        )
        if members[unit.label] != current:
            members[unit.label] = current
            changed = True
    if changed:
        inventory.write(inventory_path, members)
    return members


def adopt(
    plan: Plan, inventory_path: Path = inventory.DEFAULT_PATH,
    daemon_dir: Path = DAEMON_DIR,
    known_inventory: dict[str, inventory.Entry] | None = None,
    apply_lock_path: Path = APPLY_LOCK,
) -> dict[str, inventory.Entry]:
    """현재 설치본을 인벤토리로 승계한다 — 전부 아니면 전무, 멱등.

    절반만 승계되면 기록되지 않은 실제 plist가 D4의 철칙("인벤토리 밖은 건드리지
    않는다")에 걸려 영구 고아가 된다 (2R Codex #1). 그래서 단일 atomic write다.
    """
    with writer_locked(apply_lock_path):
        inventory._migrate_unlocked(inventory_path)
        on_disk = inventory.read(inventory_path)
        if on_disk is not None:
            known_inventory = on_disk
        elif known_inventory is None:
            known_inventory = {}
        return _adopt_unlocked(plan, inventory_path, daemon_dir, known_inventory)


def _adopt_unlocked(
    plan: Plan,
    inventory_path: Path,
    daemon_dir: Path,
    known_inventory: dict[str, inventory.Entry] | None,
) -> dict[str, inventory.Entry]:
    known_inventory = known_inventory or {}
    units = plan.create + plan.changed + plan.unchanged
    # 은퇴 대상도 승계한다. desired만 기록하면, 이미 인벤토리가 있는 상태에서
    # --adopt를 돌렸을 때 plan.retire의 plist가 설치된 채 기록에서 빠져 영구
    # 고아가 된다 — 인벤토리 밖은 건드리지 않는다는 D4 철칙 때문에 이후 어떤
    # apply도 그것을 은퇴시킬 수 없다 (2R Codex).
    unit_sources = {unit.label: unit.source for unit in units}
    members: dict[str, inventory.Entry] = {
        label: entry
        for label, entry in known_inventory.items()
        if (entry.lifecycle == "masked" or label in plan.mask)
        and label not in plan.retire
        and label not in unit_sources
    }
    # A currently-installed service that is about to be masked must be adopted
    # as active first. Dropping it here would erase ownership, so the next
    # regular apply could no longer perform the mask safely.
    labels = list(unit_sources) + list(plan.retire) + list(plan.mask)
    for label in labels:
        target = daemon_dir / f"{label}.plist"
        if target.exists():
            source = unit_sources.get(label)
            if source is None and known_inventory and label in known_inventory:
                source = known_inventory[label].source
            members[label] = inventory.Entry(
                hashlib.sha256(target.read_bytes()).hexdigest(), source, "active"
            )
    inventory.write(inventory_path, members)
    return members
