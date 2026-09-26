"""Stable JSON projection of the merged service model (D14)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path

from macosctl.model import MergedModel
from macosctl.policy import WHEEL_GID

MANIFEST_PATH = Path("/var/db/macosctl/manifest.json")

_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "T": 1024 ** 4,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
}
_U64_MAX = 2 ** 64 - 1


def _require_string(value: object, field: str, service: object = None) -> str:
    if type(value) is not str:
        prefix = f"{service}: " if service is not None else ""
        raise ValueError(f"{prefix}{field}는 string이어야 한다: {value!r}")
    return value


def _require_integer(value: object, field: str, service: object = None) -> int:
    if type(value) is not int:
        prefix = f"{service}: " if service is not None else ""
        raise ValueError(f"{prefix}{field}는 integer여야 한다: {value!r}")
    return value


def _size_bytes(value: str | None) -> int | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"mem_budget은 string 또는 null이어야 한다: {value!r}")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", value)
    if match is None or match.group(2).upper() not in _SIZE_UNITS:
        raise ValueError(f"잘못된 mem_budget: {value!r}")
    result = int(Decimal(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])
    if not 0 <= result <= _U64_MAX:
        raise ValueError(f"mem_budget이 u64 범위를 벗어난다: {value!r}")
    return result


def _service_document(service) -> dict:
    name = _require_string(service.name, "name")
    label = _require_string(service.label, "label", name)
    group = _require_string(service.group, "group", name)
    working_directory = _require_string(
        service.working_directory, "working_directory", name
    )
    port = _require_integer(service.port, "port", name)
    if type(service.managed) is not bool:
        raise ValueError(
            f"{name}: managed는 boolean이어야 한다: {service.managed!r}"
        )
    lifecycle = _require_string(service.lifecycle, "lifecycle", name)
    if lifecycle not in ("active", "masked"):
        raise ValueError(f"{name}: 잘못된 lifecycle: {lifecycle!r}")
    if service.managed and lifecycle == "masked":
        raise ValueError(f"{name}: managed:true + lifecycle:masked는 불가능하다")
    if not all(type(dependency) is str for dependency in service.depends_on):
        raise ValueError(
            f"{name}: depends_on은 string 목록이어야 한다: {service.depends_on!r}"
        )
    source = _require_string(service.source_of("name"), "source", name)
    return {
        "name": name,
        "label": label,
        "port": port,
        "group": group,
        "managed": service.managed,
        "lifecycle": lifecycle,
        "mem_budget": _size_bytes(service.mem_budget),
        "depends_on": list(service.depends_on),
        "working_directory": working_directory,
        "source": source,
    }


def manifest_json(merged: MergedModel) -> str:
    log_dir = _require_string(merged.defaults.log_dir, "log_dir")
    throttle_seconds = _require_integer(
        merged.defaults.throttle_seconds, "throttle_seconds"
    )
    document = {
        "schema": 1,
        "defaults": {
            "log_dir": log_dir,
            "throttle_seconds": throttle_seconds,
        },
        "services": [_service_document(service) for service in merged.services],
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


def write(merged: MergedModel, path: Path = MANIFEST_PATH) -> None:
    """Atomically replace the on-disk JSON projection."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        payload = manifest_json(merged).encode("utf-8")
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            if path == MANIFEST_PATH:
                os.fchown(handle.fileno(), 0, WHEEL_GID)
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
