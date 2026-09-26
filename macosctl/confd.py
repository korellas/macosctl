"""Read and merge macosctl.toml, project fragments, and machine drop-ins (D4)."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

from macosctl import model, policy
from macosctl.manifest import Defaults

CONFIG_ROOT = Path("/etc/macosctl")
DEFAULTS_BASENAME = "macosctl.toml"
SCHEMA = 1


class FragmentUnreadable(Exception):
    """A configured fragment exists by name but cannot be read."""


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except OSError as exc:
        raise FragmentUnreadable(f"조각을 읽을 수 없다: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise model.MergeError(f"TOML 파싱 실패 ({path}): {exc}") from exc


def _check_schema(raw: dict, path: Path) -> None:
    if type(raw.get("schema")) is not int or raw.get("schema") != SCHEMA:
        raise model.MergeError(
            f"지원하지 않는 schema ({path.name}): {raw.get('schema')!r}"
        )


def _reject_policy_user(raw: dict, path: Path, location: str) -> None:
    if "user" in raw:
        raise model.MergeError(
            f"user는 service 선언에서만 허용되고 policy.json의 "
            f"service_user/service_users가 유일한 정본이다 "
            f"({path.name}의 {location}.user 금지)"
        )


def _defaults(raw: dict, path: Path) -> Defaults:
    _check_schema(raw, path)
    _reject_policy_user(raw, path, "top-level")
    unknown = sorted(set(raw) - {"schema", "defaults"})
    if unknown:
        raise model.MergeError(
            f"허용되지 않은 필드 ({path.name}): {', '.join(unknown)}"
        )
    values = raw.get("defaults", {})
    if not isinstance(values, dict):
        raise model.MergeError(f"defaults는 테이블이어야 한다 ({path.name})")
    _reject_policy_user(values, path, "defaults")
    allowed = {
        "working_directory",
        "log_dir",
        "throttle_seconds",
        "path",
        "log_rotate_interval_seconds",
        "log_max_mb",
        "log_keep",
    }
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise model.MergeError(
            f"허용되지 않은 defaults 필드 ({path.name}): {', '.join(unknown)}"
        )
    return Defaults(
        # Task 4's root-owned policy reader is the only authority allowed to
        # resolve this.  None keeps the existing model boundary explicit and
        # prevents this user-owned config loader from inventing an identity.
        user=None,
        working_directory=values.get("working_directory", "/"),
        log_dir=values.get("log_dir", "/tmp"),
        throttle_seconds=int(values.get("throttle_seconds", 10)),
        path=values.get("path", "/usr/bin:/bin:/usr/sbin:/sbin"),
        log_rotate_interval_seconds=int(
            values.get("log_rotate_interval_seconds", 900)
        ),
        log_max_mb=int(values.get("log_max_mb", 20)),
        log_keep=int(values.get("log_keep", 5)),
    )


_SERVICE_FIELDS = {
    "name",
    "label",
    "port",
    "group",
    "managed",
    "exec",
    "depends_on",
    "mem_budget",
    "env",
    "working_directory",
    "process_type",
    "user",
    "log_dir",
}
_DROPIN_FIELDS = _SERVICE_FIELDS | {"schema", "unset"}
PROCESS_TYPES = ("Background", "Standard", "Adaptive", "Interactive")


def _reject_fields(raw: dict, allowed: set[str], path: Path) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise model.MergeError(
            f"허용되지 않은 필드 ({path.name}): {', '.join(unknown)}"
        )


def _service(
    entry: dict,
    defaults: Defaults,
    source: str,
    location: str | None = None,
    defaults_location: str | None = None,
) -> model.MergedService:
    _reject_fields(entry, _SERVICE_FIELDS, Path(source))
    missing = [key for key in ("name", "label", "port", "exec") if key not in entry]
    if missing:
        raise model.MergeError(f"필수 필드가 없다 ({source}): {missing}")
    requested_user = entry.get("user")
    if "user" in entry and (
        not isinstance(requested_user, str)
        or policy.USER_PATTERN.fullmatch(requested_user) is None
    ):
        raise model.MergeError(
            f"서비스 user가 잘못됐다 ({source}, {entry.get('name')!r}): "
            f"{requested_user!r}"
        )
    process_type = entry.get("process_type", "Background")
    fields = {
        "name": entry["name"],
        "label": entry["label"],
        "port": int(entry["port"]),
        "group": entry.get("group", "?"),
        "managed": bool(entry.get("managed", True)),
        "exec_argv": tuple(entry["exec"]),
        "depends_on": tuple(entry.get("depends_on", ())),
        "mem_budget": entry.get("mem_budget"),
        "env": tuple(sorted(entry.get("env", {}).items())),
        "working_directory": entry.get(
            "working_directory", defaults.working_directory
        ),
        "process_type": process_type,
        "user": requested_user,
        "log_dir": entry.get("log_dir"),
    }
    explicit = {
        "name": "name",
        "label": "label",
        "port": "port",
        "group": "group",
        "managed": "managed",
        "exec": "exec_argv",
        "depends_on": "depends_on",
        "mem_budget": "mem_budget",
        "env": "env",
        "process_type": "process_type",
        "user": "user",
        "log_dir": "log_dir",
    }
    sources = tuple(
        model.Provenance(model_field, source, location)
        for config_field, model_field in explicit.items()
        if config_field in entry
    )
    sources += (
        model.Provenance(
            "working_directory",
            source if "working_directory" in entry else "macosctl.toml",
            location if "working_directory" in entry else defaults_location,
        ),
    )
    return model.MergedService(
        **fields,
        lifecycle="active",
        sources=sources,
    )


def _dropin(
    service: model.MergedService,
    raw: dict,
    path: Path,
    defaults: Defaults,
) -> model.MergedService:
    _check_schema(raw, path)
    _reject_policy_user(raw, path, "dropin")
    _reject_fields(raw, _DROPIN_FIELDS, path)
    immutable = [field for field in ("name", "label") if field in raw]
    if immutable:
        raise model.MergeError(
            f"드롭인은 {', '.join(immutable)}을 바꿀 수 없다 ({path.name})"
        )

    unset = raw.get("unset", [])
    if not isinstance(unset, list) or not all(isinstance(item, str) for item in unset):
        raise model.MergeError(f"unset은 문자열 목록이어야 한다 ({path.name})")

    values = {
        "port": service.port,
        "group": service.group,
        "managed": service.managed,
        "exec_argv": service.exec_argv,
        "depends_on": service.depends_on,
        "mem_budget": service.mem_budget,
        "env": dict(service.env),
        "working_directory": service.working_directory,
        "process_type": service.process_type,
        "log_dir": service.log_dir,
    }
    source_updates: list[model.Provenance] = []
    conversions = {
        "port": ("port", int),
        "group": ("group", str),
        "managed": ("managed", bool),
        "exec": ("exec_argv", tuple),
        "depends_on": ("depends_on", tuple),
        "mem_budget": ("mem_budget", lambda value: value),
        "working_directory": ("working_directory", str),
        "process_type": ("process_type", str),
        "log_dir": ("log_dir", str),
    }
    for config_field, (model_field, convert) in conversions.items():
        if config_field in raw:
            values[model_field] = convert(raw[config_field])
            source_updates.append(model.Provenance(model_field, path.name, str(path)))
    if "env" in raw:
        if not isinstance(raw["env"], dict):
            raise model.MergeError(f"env는 테이블이어야 한다 ({path.name})")
        values["env"].update(raw["env"])
        source_updates.append(model.Provenance("env", path.name, str(path)))

    reset = {
        "group": "?",
        "managed": True,
        "depends_on": (),
        "mem_budget": None,
        "env": {},
        "working_directory": defaults.working_directory,
        "process_type": "Background",
        "log_dir": None,
    }
    for field in unset:
        if field.startswith("env."):
            key = field.removeprefix("env.")
            if not key:
                raise model.MergeError(f"잘못된 unset 항목 ({path.name}): {field}")
            values["env"].pop(key, None)
            source_updates.append(model.Provenance("env", path.name, str(path)))
            continue
        if field not in reset:
            raise model.MergeError(
                f"선택 필드만 unset할 수 있다 ({path.name}): {field}"
            )
        values[field] = reset[field]
        source_updates.append(model.Provenance(field, path.name, str(path)))

    lifecycle = service.lifecycle
    if "managed" in raw:
        lifecycle = "masked" if values["managed"] is False else "active"
    if "managed" in unset:
        lifecycle = "active"

    return replace(
        service,
        port=values["port"],
        group=values["group"],
        managed=values["managed"],
        exec_argv=values["exec_argv"],
        depends_on=values["depends_on"],
        mem_budget=values["mem_budget"],
        env=tuple(sorted(values["env"].items())),
        working_directory=values["working_directory"],
        process_type=values["process_type"],
        log_dir=values["log_dir"],
        lifecycle=lifecycle,
        sources=service.sources + tuple(source_updates),
    )


def load(config_root: Path = CONFIG_ROOT) -> model.MergedModel:
    config_root = Path(config_root)
    defaults_path = config_root / DEFAULTS_BASENAME
    defaults = _defaults(_read(defaults_path), defaults_path)
    services: list[model.MergedService] = []
    origins: dict[str, str] = {}
    for path in sorted((config_root / "conf.d").glob("*.toml")):
        raw = _read(path)
        _check_schema(raw, path)
        _reject_policy_user(raw, path, "fragment")
        _reject_fields(raw, {"schema", "service", "scaffold"}, path)
        if "scaffold" in raw and not isinstance(raw["scaffold"], dict):
            raise model.MergeError(f"scaffold는 테이블이어야 한다 ({path.name})")
        if "scaffold" in raw:
            _reject_policy_user(raw["scaffold"], path, "scaffold")
        for entry in raw.get("service", []):
            name = entry.get("name")
            if name in origins:
                raise model.MergeError(
                    f"서비스 {name!r} 중복 선언: {origins[name]}, {path.name}"
                )
            service = _service(
                entry,
                defaults,
                path.name,
                str(path),
                str(defaults_path),
            )
            origins[service.name] = path.name
            services.append(service)

    warnings: list[str] = []
    by_name = {service.name: index for index, service in enumerate(services)}
    for directory in sorted((config_root / "conf.d").glob("*.d")):
        if not directory.is_dir():
            continue
        name = directory.name.removesuffix(".d")
        if name not in by_name:
            warnings.append(model.orphan_dropin_warning(name))
            continue
        index = by_name[name]
        for path in sorted(directory.glob("*.toml")):
            services[index] = _dropin(services[index], _read(path), path, defaults)

    return model.MergedModel(defaults, tuple(services), tuple(warnings))
