"""Human and machine-readable service status views."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from macosctl import collect, model


ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\033[0m"

STATE_STYLE = {
    "running": ("●", "RUNNING", "\033[1;32m"),
    "down": ("○", "DOWN", "\033[31m"),
    "rogue": ("▲", "ROGUE", "\033[1;33m"),
    "missing": ("×", "MISSING", "\033[1;31m"),
    "masked": ("◎", "MASKED", "\033[95m"),
    "external": ("◇", "EXTERNAL", "\033[36m"),
}
ISSUE_STATES = frozenset({"down", "rogue", "missing"})
INTENTIONAL_STATES = frozenset({"masked", "external"})


@dataclass(frozen=True)
class ListenerView:
    pid: int
    command: str
    owned: bool


@dataclass(frozen=True)
class Row:
    name: str
    label: str
    state: str
    detail: str
    port: int
    pid: int | None
    group: str
    managed: bool
    lifecycle: str
    installed: bool
    job_loaded: bool
    job_state: str | None
    job_pid: int | None
    listeners: tuple[ListenerView, ...]
    disabled_override: bool | None
    source: str | None


def _classify(
    service: model.MergedService,
    observed: collect.SystemState,
) -> Row:
    job = observed.jobs.get(service.label)
    raw_listeners = observed.listeners_on(service.port)
    job_pid = job.pid if job else None
    listeners = tuple(
        ListenerView(
            pid=listener.pid,
            command=listener.command,
            owned=bool(job_pid and observed.is_descendant(listener.pid, job_pid)),
        )
        for listener in raw_listeners
    )
    owned = any(listener.owned for listener in listeners)
    installed = service.label in observed.installed_labels

    if service.lifecycle == "masked":
        state, detail = "masked", "intentionally suppressed"
    elif owned:
        state, detail = "running", "healthy"
    elif listeners and service.managed:
        state, detail = "rogue", "foreign listener owns port"
    elif not service.managed:
        state = "external"
        detail = "observed only" if listeners else "not running, unmanaged"
    elif not installed:
        state, detail = "missing", "apply required"
    else:
        state = "down"
        detail = "job loaded, process absent" if job and job.loaded else "process absent"

    owned_listener = next((listener for listener in listeners if listener.owned), None)
    pid = owned_listener.pid if owned_listener else (listeners[0].pid if listeners else job_pid)
    return Row(
        name=service.name,
        label=service.label,
        state=state,
        detail=detail,
        port=service.port,
        pid=pid,
        group=service.group,
        managed=service.managed,
        lifecycle=service.lifecycle,
        installed=installed,
        job_loaded=bool(job and job.loaded),
        job_state=job.state if job else None,
        job_pid=job_pid,
        listeners=listeners,
        disabled_override=observed.disabled_overrides.get(service.label),
        source=service.source_of("name"),
    )


def build_rows(
    services: Iterable[model.MergedService],
    observed: collect.SystemState,
) -> tuple[Row, ...]:
    """Project the immutable merged model and one live snapshot into status rows."""
    return tuple(_classify(service, observed) for service in services)


def summary(rows: Iterable[Row]) -> dict[str, int]:
    materialized = tuple(rows)
    return {
        "total": len(materialized),
        "running": sum(row.state == "running" for row in materialized),
        "issues": sum(row.state in ISSUE_STATES for row in materialized),
        "intentional": sum(row.state in INTENTIONAL_STATES for row in materialized),
        "overrides": sum(row.disabled_override is True for row in materialized),
    }


def _row_dict(row: Row) -> dict[str, object]:
    return {
        "name": row.name,
        "label": row.label,
        "state": row.state,
        "detail": row.detail,
        "port": row.port,
        "pid": row.pid,
        "group": row.group,
        "managed": row.managed,
        "lifecycle": row.lifecycle,
        "installed": row.installed,
        "job": {
            "loaded": row.job_loaded,
            "state": row.job_state,
            "pid": row.job_pid,
        },
        "listeners": [
            {"pid": listener.pid, "command": listener.command, "owned": listener.owned}
            for listener in row.listeners
        ],
        "disabled_override": row.disabled_override,
        "source": row.source,
    }


def render_json(rows: Iterable[Row]) -> str:
    materialized = tuple(rows)
    payload = {
        "schema": 1,
        "summary": summary(materialized),
        "services": [_row_dict(row) for row in materialized],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _visible_length(value: str) -> int:
    return len(ANSI.sub("", value))


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def _pad(value: str, width: int, alignment: str = "left") -> str:
    spaces = " " * max(0, width - _visible_length(value))
    return spaces + value if alignment == "right" else value + spaces


def _paint(value: str, escape: str, color: bool) -> str:
    return f"{escape}{value}{RESET}" if color else value


def _layout(width: int) -> str:
    if width >= 96:
        return "full"
    if width >= 64:
        return "standard"
    return "compact"


def _fit_columns(columns: list[dict[str, object]], width: int) -> None:
    """Shrink descriptive columns until the table fits the current terminal."""
    overflow = sum(int(column["width"]) for column in columns) + 2 * (len(columns) - 1) - width
    for key in ("DETAIL", "GROUP", "SERVICE"):
        column = next((item for item in columns if item["label"] == key), None)
        if column is None or overflow <= 0:
            continue
        reducible = int(column["width"]) - int(column["minimum"])
        reduction = min(overflow, max(0, reducible))
        column["width"] = int(column["width"]) - reduction
        overflow -= reduction


def render_text(rows: Iterable[Row], *, width: int, color: bool) -> str:
    materialized = tuple(rows)
    layout = _layout(width)
    state_width = max(len(f"{glyph} {label}") for glyph, label, _ in STATE_STYLE.values())
    name_width = max([len("SERVICE"), *(len(row.name) for row in materialized)])
    port_width = max([len("PORT"), *(len(str(row.port)) for row in materialized)])
    pid_width = max([len("PID"), *(len(str(row.pid)) if row.pid else 1 for row in materialized)])
    group_width = max([len("GROUP"), *(len(row.group) for row in materialized)])
    detail_width = max([len("DETAIL"), *(len(row.detail) for row in materialized)])

    columns: list[dict[str, object]] = [
        {"label": "STATE", "width": state_width, "minimum": state_width, "align": "left"},
        {"label": "SERVICE", "width": name_width, "minimum": 4, "align": "left"},
        {"label": "PORT", "width": port_width, "minimum": port_width, "align": "right"},
    ]
    if width >= 44:
        columns.append({"label": "PID", "width": pid_width, "minimum": pid_width, "align": "right"})
    if layout != "compact":
        columns.append({"label": "GROUP", "width": group_width, "minimum": 3, "align": "left"})
    if layout == "full":
        columns.append({"label": "DETAIL", "width": detail_width, "minimum": 8, "align": "left"})
    _fit_columns(columns, width)

    def format_values(values: list[tuple[str, str | None]]) -> str:
        rendered = []
        for (value, escape), column in zip(values, columns, strict=True):
            clipped = _truncate(value, int(column["width"]))
            painted = _paint(clipped, escape, color) if escape else clipped
            rendered.append(_pad(painted, int(column["width"]), str(column["align"])))
        return "  ".join(rendered).rstrip()

    header = format_values([(str(column["label"]), "\033[1m") for column in columns])
    output_rows = []
    for row in materialized:
        glyph, label, escape = STATE_STYLE[row.state]
        values: list[tuple[str, str | None]] = [
            (f"{glyph} {label}", escape),
            (row.name, None),
            (str(row.port), None),
        ]
        if width >= 44:
            values.append((str(row.pid) if row.pid is not None else "-", None))
        if layout != "compact":
            values.append((row.group, None))
        if layout == "full":
            values.append((row.detail, "\033[2m"))
        output_rows.append(format_values(values))

    counts = summary(materialized)
    title = _paint("macosctl  SERVICES", "\033[1;36m", color)
    primary = (
        f"{counts['running']} running · {counts['issues']} issues · "
        f"{counts['intentional']} intentional"
    )
    # 사람용 명칭은 "무엇이 막혔는지"를 말한다. JSON의 summary.overrides 키는
    # Webtop이 소비하므로 schema 1 그대로 둔다 (_row_dict/summary 참조).
    secondary = f"{counts['overrides']} boot-disabled"
    combined = f"{primary}    {secondary}"
    summary_lines = [combined] if len(combined) <= width else [primary, secondary]
    table_width = max([_visible_length(header), *map(_visible_length, output_rows)], default=0)
    heavy = _paint("━" * min(width, table_width), "\033[90m", color)
    light = _paint("─" * min(width, table_width), "\033[90m", color)
    return "\n".join([title, *summary_lines, heavy, header, light, *output_rows, heavy]) + "\n"
