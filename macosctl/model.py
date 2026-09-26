"""불변 병합 모델 — 파일은 한 번만 읽고 이 객체만 돌아다닌다 (D4-10)."""

from __future__ import annotations

from dataclasses import dataclass

from macosctl.manifest import Defaults


ORPHAN_DROPIN_PREFIX = "orphan-dropin: 대상 서비스가 없다: "


def orphan_dropin_warning(name: str) -> str:
    return f"{ORPHAN_DROPIN_PREFIX}{name} ({name}.d)"


def parse_orphan_dropin_warning(warning: str) -> tuple[str, str] | None:
    """Parse only warnings produced by ``orphan_dropin_warning``.

    The service-directory basename may contain whitespace or parentheses, so
    splitting at the first parenthesis is not valid. Reconstructing the exact
    formatter output makes the human string a checked transport rather than a
    permissive ad-hoc grammar.
    """
    if not warning.startswith(ORPHAN_DROPIN_PREFIX):
        return None
    body = warning.removeprefix(ORPHAN_DROPIN_PREFIX)
    for index in range(len(body)):
        if not body.startswith(" (", index) or not body.endswith(")"):
            continue
        name = body[:index]
        directory = body[index + 2:-1]
        if directory == f"{name}.d" and warning == orphan_dropin_warning(name):
            return name, directory
    return None


class MergeError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Provenance:
    field: str
    source: str
    location: str | None = None


@dataclass(frozen=True)
class MergedService:
    name: str
    label: str
    port: int
    group: str
    managed: bool
    exec_argv: tuple[str, ...]
    depends_on: tuple[str, ...]
    mem_budget: str | None
    env: tuple[tuple[str, str], ...]
    working_directory: str
    lifecycle: str
    sources: tuple[Provenance, ...]
    process_type: str = "Background"
    user: str | None = None
    log_dir: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "exec_argv", tuple(self.exec_argv))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "env", tuple(tuple(item) for item in self.env))
        object.__setattr__(self, "sources", tuple(self.sources))

    def source_of(self, field: str) -> str | None:
        """그 필드를 최종 결정한 파일을 돌려준다."""
        return next(
            (item.source for item in reversed(self.sources) if item.field == field),
            None,
        )

    def location_of(self, field: str) -> str | None:
        """Return the winning full config location, falling back for v1 callers."""
        return next(
            (
                item.location or item.source
                for item in reversed(self.sources)
                if item.field == field
            ),
            None,
        )


@dataclass(frozen=True)
class MergedModel:
    defaults: Defaults
    services: tuple[MergedService, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "services", tuple(self.services))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def by_name(self, name: str) -> MergedService | None:
        return next((service for service in self.services if service.name == name), None)

    def managed_only(self) -> tuple[MergedService, ...]:
        return tuple(service for service in self.services if service.managed)
