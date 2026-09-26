"""설치 인벤토리 — 무엇을 우리가 설치했는지에 대한 root 소유 기록.

설계 근거: docs/design.md

**왜 접두사가 아니라 인벤토리인가.** `com.korellas.*`를 전부 우리 것으로 보고
매니페스트에 없으면 은퇴시키면, 매니페스트 밖에서 정당하게 설치되는
`com.korellas.log-rotate`가 매 apply마다 죽는다. 접두사는 소유권 증명이 아니다.
같은 이유로 macosctl-helper의 제어 대상 판정도 "root 소유 plist가 있다"가 아니라 이
인벤토리 멤버십이어야 한다 (D2) — root 소유는 **누가 설치했는지**를 증명하지 않는다.

**갱신 규약** (2R Codex #1 / Fable H-4):
  · 멤버십 기준은 "plist가 설치되어 있음"이며 bootstrap 성공 여부와 무관하다.
  · 매 apply가 desired set 전체를 다시 쓴다 — 변경 없어 건드리지 않은 서비스도
    매번 포함한다. 그래야 "plist는 설치됐는데 기록 전 크래시" 후 재실행이
    수렴한다 (안 그러면 영구 은퇴 불가 고아가 된다).
  · 쓰기는 임시파일 + atomic rename.

**fail-closed.** 손상·부재를 빈 인벤토리로 읽으면 은퇴 기능만 조용히 죽어 드리프트가
누적된다. 부재(None)와 빈 것({})을 구분하고, 손상은 예외를 던진다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import state
from .policy import LABEL_PATTERN

DEFAULT_PATH = Path("/var/db/macosctl/inventory")
FORMAT_VERSION = 2
BACKUP_SUFFIX = ".v1.bak"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Entry:
    sha256: str | None
    source: str | None
    lifecycle: str

    def __post_init__(self) -> None:
        if self.lifecycle not in ("active", "masked"):
            raise ValueError(f"알 수 없는 lifecycle: {self.lifecycle!r}")
        if self.source is not None and not isinstance(self.source, str):
            raise TypeError("source는 문자열 또는 null이어야 한다")
        if self.lifecycle == "active":
            if (
                not isinstance(self.sha256, str)
                or SHA256_PATTERN.fullmatch(self.sha256) is None
            ):
                raise ValueError(
                    "active 항목의 sha256은 lowercase 64-hex여야 한다"
                )
        elif self.sha256 is not None:
            raise ValueError("masked 항목의 sha256은 null이어야 한다")


class InventoryCorrupt(Exception):
    """인벤토리 내용이 깨졌다 — 복구(`--rebuild-inventory`)가 필요한 상태."""


class InventoryUnreadable(Exception):
    """인벤토리를 읽을 권한이 없다.

    손상과 구분한다. 파일은 root 소유이므로 접근 실패는 '권한이 부족하다'는
    뜻이지 '내용이 깨졌다'는 뜻이 아니고, 안내해야 할 조치가 전혀 다르다.
    """


def _read_raw(path: Path) -> tuple[bytes, object] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise InventoryUnreadable(
            f"인벤토리를 읽을 권한이 없다 ({path}): {exc}"
        ) from exc
    except OSError as exc:
        raise InventoryCorrupt(f"인벤토리를 읽을 수 없다 ({path}): {exc}") from exc

    try:
        data = json.loads(raw)
        return raw, data
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InventoryCorrupt(
            f"인벤토리가 손상됐다 ({path}): {exc} — "
            f"`macosctl apply --rebuild-inventory`로 복구할 것"
        ) from exc


def _parse(path: Path, data: object) -> tuple[int, dict[str, Entry]]:
    try:
        if not isinstance(data, dict):
            raise TypeError("최상위 값이 객체가 아니다")
        version = data["version"]
        if type(version) is not int or version not in (1, FORMAT_VERSION):
            raise TypeError(f"지원하지 않는 인벤토리 버전: {version!r}")
        labels = data["labels"]
        if not isinstance(labels, dict):
            raise TypeError("labels가 객체가 아니다")
        entries: dict[str, Entry] = {}
        for label, value in labels.items():
            if (
                not isinstance(label, str)
                or LABEL_PATTERN.fullmatch(label) is None
            ):
                raise TypeError(f"label 문법이 잘못됐다: {label!r}")
            if version == 1:
                if not isinstance(value, str):
                    raise TypeError(f"v1 항목이 문자열이 아니다: {label}")
                entries[label] = Entry(value, None, "active")
                continue
            if not isinstance(value, dict):
                raise TypeError(f"v2 항목이 객체가 아니다: {label}")
            if set(value) != {"sha256", "source", "lifecycle"}:
                raise TypeError(f"v2 항목 필드가 잘못됐다: {label}")
            entries[label] = Entry(
                value["sha256"], value["source"], value["lifecycle"]
            )
        return version, entries
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryCorrupt(
            f"인벤토리가 손상됐다 ({path}): {exc} — "
            f"`macosctl apply --rebuild-inventory`로 복구할 것"
        ) from exc


def read(path: Path = DEFAULT_PATH) -> dict[str, Entry] | None:
    """항목을 v2 모델로 읽는다. 파일이 없으면 None (빈 것과 다르다)."""
    loaded = _read_raw(path)
    if loaded is None:
        return None
    _, data = loaded
    _, entries = _parse(path, data)
    return entries


def _payload(entries: dict[str, Entry]) -> bytes:
    labels = {}
    for label, entry in sorted(entries.items()):
        if (
            not isinstance(label, str)
            or LABEL_PATTERN.fullmatch(label) is None
        ):
            raise ValueError(f"label 문법이 잘못됐다: {label!r}")
        if not isinstance(entry, Entry):
            raise TypeError(f"인벤토리 항목은 Entry여야 한다: {entry!r}")
        labels[label] = asdict(entry)
    return (json.dumps(
        {"version": FORMAT_VERSION, "labels": labels},
        indent=2,
        ensure_ascii=False,
    ) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".inventory-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            # 권한 메타데이터까지 파일 fsync보다 먼저 정한다. rename 직후 전원이
            # 나가도 일반 사용자가 읽을 수 있는 0644 계약이 함께 내구화된다.
            os.fchmod(fh.fileno(), 0o644)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write(path: Path, entries: dict[str, Entry]) -> None:
    """전체를 v2로 직렬화해 임시파일 + atomic rename으로 갈아끼운다."""
    _write_atomic(path, _payload(entries))


def _migrate_unlocked(path: Path) -> bool:
    """v1을 v2로 승격한다. 호출자가 apply writer 락을 단 한 번 소유해야 한다.

    Task 8의 전체 apply 구간에서는 이 core를 호출한다. public ``migrate``를 외부
    락 안에서 부르면 macOS flock의 비재진입 특성 때문에 자기교착한다.
    """
    loaded = _read_raw(path)
    if loaded is None:
        return False
    original, data = loaded
    version, entries = _parse(path, data)
    if version == FORMAT_VERSION:
        return False

    # 백업 rename과 그 디렉터리 엔트리를 먼저 내구화한다. 이 순서를 지켜야
    # 원본을 v2로 교체한 직후 전원이 나가도 롤백 다리가 남는다 (D7-4).
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    _write_atomic(backup, original)
    # version=2인 새 inventory 자체가 완료 marker다. atomic rename 뒤 부모
    # 디렉터리까지 fsync하므로 marker가 보이면 승격본 전체도 함께 내구적이다.
    _write_atomic(path, _payload(entries))
    return True


def migrate(path: Path, lock_path: Path) -> bool:
    """독립 호출용 락 owner. 이미 apply.lock을 쥔 코드는 core를 호출한다."""
    with state.locked(lock_path):
        return _migrate_unlocked(path)
