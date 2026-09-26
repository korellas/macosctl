"""desired state — macosctl이 남긴 **의도**만 기록한다 (v2).

설계 근거: docs/design.md

정본 분리가 이 파일의 전부다:

  · 실제 boot policy 정본 = `launchctl print-disabled`
  · 실제 runtime 정본     = launchd job/PID + 포트 소유권
  · 이 파일                = (a) macosctl이 disable했다는 **출처**와
                             (b) **현재 boot session에 한정된** stop 의도

runtime 사실은 여기 저장하지 않는다. 실제 disabled와 provenance는 같은 사실의
중복 정본이 아니다 — launchd가 실제 정책이고, 이 파일은 누가 그렇게 만들었는지만
말한다. 그래서 apply는 provenance가 있든 없든 **실제** disabled를 보존한다.

**stop의 수명은 부팅까지다.** bootout은 런타임만 지우고 plist는 남으므로
RunAtLoad가 재부팅 시 되살린다. 그래서 stop 의도는 boot session UUID에 묶어
기록하고, UUID가 달라지면 자동 만료시킨다 — 낡은 마크가 다음 부팅의 기동을
막는 일이 없다. 재부팅을 넘기려면 disable을 쓴다.

**락.** 파일 쓰기의 원자성만으로는 부족하다. 실제 경쟁은 read-then-act에서 난다:
apply가 state를 읽어 "의도 없음"으로 판단한 뒤, 그 사이 macosctl-helper이 stop
의도를 기록하고 bootout하고, 낡은 스냅샷을 든 apply가 bootstrap하면 state는
stopped인데 서비스는 running이 된다. apply와 macosctl-helper이 같은 락을 잡아야 한다.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

DEFAULT_PATH = Path("/var/db/macosctl/state")
DEFAULT_LOCK = Path("/var/db/macosctl/lock")

VERSION = 2
SUPPORTED_VERSIONS = (1, 2)


class StateCorrupt(Exception):
    """state 파일을 해석할 수 없다."""


def _frozen_mapping(items: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(sorted(items.items())))


@dataclass(frozen=True)
class State:
    """불변 스냅샷. 두 축은 서로를 지우지 않는다."""

    disabled_by_macosctl: frozenset[str] = frozenset()
    stopped: Mapping[str, str] = field(default_factory=lambda: _frozen_mapping({}))

    def is_stopped_this_boot(self, label: str, boot_uuid: str | None) -> bool:
        """현재 boot session에 귀속된 stop 의도인가.

        boot_uuid를 관측할 수 없으면 의도를 주장하지 않는다 — 관측 불가를 "정지
        의도 있음"으로 읽으면 apply가 조용히 기동을 건너뛴다. 실제 fail-closed는
        관측 자체가 필요한 호출자(apply)가 자기 자리에서 한다.
        """
        return boot_uuid is not None and self.stopped.get(label) == boot_uuid

    def stopped_this_boot(self, boot_uuid: str | None) -> frozenset[str]:
        if boot_uuid is None:
            return frozenset()
        return frozenset(
            label for label, uuid in self.stopped.items() if uuid == boot_uuid
        )

    def with_stopped(self, label: str, boot_uuid: str) -> "State":
        return State(
            self.disabled_by_macosctl,
            _frozen_mapping({**self.stopped, label: boot_uuid}),
        )

    def without_stopped(self, label: str) -> "State":
        return State(
            self.disabled_by_macosctl,
            _frozen_mapping({k: v for k, v in self.stopped.items() if k != label}),
        )

    def with_disabled(self, label: str) -> "State":
        return State(self.disabled_by_macosctl | {label}, self.stopped)

    def without_disabled(self, label: str) -> "State":
        return State(self.disabled_by_macosctl - {label}, self.stopped)


EMPTY = State()


# --- boot session --------------------------------------------------------------


def boot_session_uuid() -> str | None:
    """현재 부팅의 식별자. 관측할 수 없으면 None (호출자가 fail-closed 판단)."""
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


# --- 락 -------------------------------------------------------------------------


@contextmanager
def locked(lock_path: Path = DEFAULT_LOCK):
    """apply와 macosctl-helper이 공유하는 advisory lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def try_lock_is_held(lock_path: Path) -> bool:
    """다른 누군가가 락을 쥐고 있는가 (테스트·진단용)."""
    if not lock_path.exists():
        return False
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# --- 읽기 -----------------------------------------------------------------------


V1_FIELDS = frozenset({"version", "marks"})
V2_FIELDS = frozenset({"version", "disabled_by_macosctl", "stopped"})
V1_MARKS = frozenset({"stopped", "disabled"})


def _require_fields(payload: dict, expected: frozenset[str], path: Path) -> None:
    """정확히 이 필드들이어야 한다 — 누락도 추가도 손상이다.

    추가 필드를 눈감으면 오타로 만들어진 별도 축(`disabled_by_macosctl_`)이
    조용히 무시되고, 그 파일을 읽은 쪽이 "의도 없음"으로 판단해 곧 덮어쓴다.
    """
    present = set(payload)
    missing = expected - present
    if missing:
        raise StateCorrupt(
            f"state가 손상됐다 ({path}): 필수 필드가 없다 {sorted(missing)}"
        )
    unknown = present - expected
    if unknown:
        raise StateCorrupt(
            f"state가 손상됐다 ({path}): 모르는 필드가 있다 {sorted(unknown)}"
        )


def _normalize_v1(payload: dict, path: Path, boot_uuid: str | None) -> State:
    """v1 {label: stopped|disabled}를 v2 두 축으로 나눈다.

    legacy stopped는 어느 부팅의 의도였는지 기록이 없다. 현재 boot session에
    귀속시켜 두 번째 부팅에서 만료되게 하고, 그 사이의 오귀속은 실제 active가
    이기고(apply의 활성화 공식) apply가 stale intent를 정리해 해소한다.
    """
    _require_fields(payload, V1_FIELDS, path)
    marks = payload["marks"]
    if not isinstance(marks, dict):
        raise StateCorrupt(f"state가 손상됐다 ({path}): marks가 객체가 아니다")

    disabled, legacy_stopped = set(), []
    for label, value in marks.items():
        if not isinstance(label, str) or not isinstance(value, str):
            raise StateCorrupt(f"state가 손상됐다 ({path}): marks 항목 타입 오류")
        if value not in V1_MARKS:
            raise StateCorrupt(f"state가 손상됐다 ({path}): 알 수 없는 마크 {value!r}")
        if value == "disabled":
            disabled.add(label)
        else:
            legacy_stopped.append(label)

    if not legacy_stopped:
        # disabled는 부팅과 무관한 축이다 — UUID 없이도 해석에 문제가 없다.
        return State(frozenset(disabled), _frozen_mapping({}))

    if boot_uuid is None:
        boot_uuid = boot_session_uuid()
    if boot_uuid is None:
        # 내용이 깨진 것은 아니지만 **의미를 정할 수 없다**. 빈 의도로 낮추면,
        # 그 판단으로 mutation을 끝낸 호출자가 v2로 승격하면서 legacy stop 의도를
        # 조용히 지운다. 해석 불가를 그대로 올려 호출자가 fail-closed 하게 한다.
        raise StateCorrupt(
            f"state를 정규화할 수 없다 ({path}): v1 stopped {sorted(legacy_stopped)}를 "
            f"귀속시킬 boot session UUID를 관측할 수 없다"
        )
    return State(
        frozenset(disabled),
        _frozen_mapping({label: boot_uuid for label in legacy_stopped}),
    )


def _normalize_v2(payload: dict, path: Path) -> State:
    _require_fields(payload, V2_FIELDS, path)
    raw_disabled = payload["disabled_by_macosctl"]
    raw_stopped = payload["stopped"]

    if not isinstance(raw_disabled, list):
        raise StateCorrupt(
            f"state가 손상됐다 ({path}): disabled_by_macosctl이 리스트가 아니다"
        )
    if not isinstance(raw_stopped, dict):
        raise StateCorrupt(f"state가 손상됐다 ({path}): stopped가 객체가 아니다")
    if not all(isinstance(label, str) for label in raw_disabled):
        raise StateCorrupt(
            f"state가 손상됐다 ({path}): disabled_by_macosctl 항목 타입 오류"
        )
    if not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_stopped.items()
    ):
        raise StateCorrupt(f"state가 손상됐다 ({path}): stopped 항목 타입 오류")
    return State(frozenset(raw_disabled), _frozen_mapping(raw_stopped))


def read(path: Path = DEFAULT_PATH, *, boot_uuid: str | None = None) -> State:
    """v1/v2를 읽어 v2 스냅샷으로 정규화한다.

    **파일 없음만 빈 State다.** 그 밖의 어떤 실패도 StateCorrupt로 올린다 — 읽지
    못한 것을 "의도가 없다"로 낮추면 호출자가 그 판단으로 정본을 덮어쓴다.
    관측 경로(collect/status)의 관용은 그쪽에서 예외를 삼켜 유지한다.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return EMPTY
    except OSError as exc:
        raise StateCorrupt(f"state를 읽을 수 없다 ({path}): {exc}") from exc

    try:
        # UnicodeDecodeError는 ValueError라 OSError 핸들러에 걸리지 않는다 —
        # bytes로 읽고 여기서 명시적으로 잡아야 손상으로 정규화된다.
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateCorrupt(f"state가 손상됐다 ({path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise StateCorrupt(f"state가 손상됐다 ({path}): 최상위가 객체가 아니다")

    version = payload.get("version")
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise StateCorrupt(f"지원하지 않는 state 버전 ({path}): {version!r}")
    if version == 1:
        return _normalize_v1(payload, Path(path), boot_uuid)
    return _normalize_v2(payload, Path(path))


# --- 쓰기 -----------------------------------------------------------------------


def write(path: Path, value: State) -> None:
    """v2로만 쓴다 — v1을 읽었더라도 다음 쓰기에서 승격된다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": VERSION,
            "disabled_by_macosctl": sorted(value.disabled_by_macosctl),
            "stopped": dict(sorted(value.stopped.items())),
        },
        indent=2,
    ) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # 조회는 일반 유저도 한다 (쓰기만 root)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update(path: Path, boot_uuid: str | None, transform) -> None:
    current = read(path, boot_uuid=boot_uuid)
    updated = transform(current)
    if updated != current or not Path(path).exists():
        write(path, updated)


def record_stopped(path: Path, label: str, boot_uuid: str) -> None:
    """현재 boot session에 한정된 stop 의도를 기록한다."""
    if not boot_uuid:
        raise ValueError("boot session UUID 없이 stop 의도를 기록할 수 없다")
    _update(path, boot_uuid, lambda s: s.with_stopped(label, boot_uuid))


def clear_stopped(path: Path, label: str, *, boot_uuid: str | None = None) -> None:
    _update(path, boot_uuid, lambda s: s.without_stopped(label))


def record_disabled(path: Path, label: str, *, boot_uuid: str | None = None) -> None:
    """macosctl이 disable했다는 출처를 남긴다 (실제 정책은 launchd가 갖는다)."""
    _update(path, boot_uuid, lambda s: s.with_disabled(label))


def clear_disabled(path: Path, label: str, *, boot_uuid: str | None = None) -> None:
    _update(path, boot_uuid, lambda s: s.without_disabled(label))


def forget(path: Path, label: str, *, boot_uuid: str | None = None) -> None:
    """은퇴한 label의 두 축을 모두 지운다 — 과거 의도를 상속시키지 않는다."""
    _update(
        path, boot_uuid,
        lambda s: s.without_stopped(label).without_disabled(label),
    )
