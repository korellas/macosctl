"""Root-owned authorization and identity policy (D2)."""

from __future__ import annotations

import json
import os
import re
import stat
try:
    import grp
except ImportError:  # pragma: no cover - grp exists on the supported macOS host
    grp = None
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from macosctl.model import MergedModel

POLICY_PATH = Path("/etc/macosctl/policy.json")
SCHEMA = 1
MAX_POLICY_BYTES = 64 * 1024
try:
    WHEEL_GID = grp.getgrnam("wheel").gr_gid if grp is not None else 0
except KeyError:  # pragma: no cover - portability for non-macOS test hosts
    WHEEL_GID = 0

LABEL_PREFIX_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){1,8}$"
)
LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?){1,8}$"
)
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class PolicyRefused(Exception):
    """The root-owned policy could not be trusted or validated."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Policy:
    label_prefix: str
    label_exceptions: Mapping[str, str]
    service_user: str
    groups: tuple[str, ...]
    service_users: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "label_exceptions",
            MappingProxyType(dict(self.label_exceptions)),
        )
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(
            self,
            "service_users",
            MappingProxyType(dict(self.service_users)),
        )


def _parse(raw: bytes, path: Path) -> Policy:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PolicyRefused(f"정책 JSON 파싱 실패 ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyRefused(f"정책 JSON은 객체여야 한다 ({path})")
    if type(data.get("schema")) is not int or data.get("schema") != SCHEMA:
        raise PolicyRefused(
            f"지원하지 않는 policy schema ({path}): {data.get('schema')!r}"
        )
    for field in ("label_prefix", "label_exceptions", "service_user", "groups"):
        if field not in data:
            raise PolicyRefused(f"정책 필수 필드가 없다 ({path}): {field}")

    prefix = data["label_prefix"]
    if not isinstance(prefix, str) or LABEL_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise PolicyRefused(f"잘못된 label_prefix ({path}): {prefix!r}")

    exceptions = data["label_exceptions"]
    if not isinstance(exceptions, dict):
        raise PolicyRefused(f"label_exceptions는 객체여야 한다 ({path})")
    for name, label in exceptions.items():
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise PolicyRefused(
                f"잘못된 label_exceptions 서비스 이름 ({path}): {name!r}"
            )
        if not isinstance(label, str) or LABEL_PATTERN.fullmatch(label) is None:
            raise PolicyRefused(
                f"잘못된 label_exceptions 라벨 ({path}, {name}): {label!r}"
            )

    service_user = data["service_user"]
    if (
        not isinstance(service_user, str)
        or USER_PATTERN.fullmatch(service_user) is None
    ):
        raise PolicyRefused(f"잘못된 service_user ({path}): {service_user!r}")

    service_users = data.get("service_users", {})
    if not isinstance(service_users, dict):
        raise PolicyRefused(f"service_users는 객체여야 한다 ({path})")
    for name, user in service_users.items():
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise PolicyRefused(
                f"잘못된 service_users 서비스 이름 ({path}): {name!r}"
            )
        if not isinstance(user, str) or USER_PATTERN.fullmatch(user) is None:
            raise PolicyRefused(
                f"잘못된 service_users 사용자 ({path}, {name}): {user!r}"
            )

    groups = data["groups"]
    if not isinstance(groups, list):
        raise PolicyRefused(f"groups는 문자열 목록이어야 한다 ({path})")
    if not all(
        isinstance(group, str) and NAME_PATTERN.fullmatch(group) is not None
        for group in groups
    ):
        raise PolicyRefused(f"groups에 잘못된 값이 있다 ({path}): {groups!r}")
    if len(set(groups)) != len(groups):
        raise PolicyRefused(f"groups에 중복이 있다 ({path}): {groups!r}")

    return Policy(
        label_prefix=prefix,
        label_exceptions=MappingProxyType(dict(exceptions)),
        service_user=service_user,
        groups=tuple(groups),
        service_users=MappingProxyType(dict(service_users)),
    )


def resolve_label(name: str, policy: Policy) -> str | None:
    if not isinstance(name, str):
        return None
    exception = policy.label_exceptions.get(name)
    if exception is not None:
        return exception if LABEL_PATTERN.fullmatch(exception) is not None else None
    if NAME_PATTERN.fullmatch(name) is None:
        return None
    label = f"{policy.label_prefix}{name}"
    return label if LABEL_PATTERN.fullmatch(label) is not None else None


def _read(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int | None = None,
    expected_directory_uid: int | None = None,
    expected_directory_gid: int | None = None,
) -> Policy:
    """Open the directory and policy once, then inspect and read those FDs."""
    path = Path(path)
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    directory_uid = (
        expected_uid if expected_directory_uid is None else expected_directory_uid
    )
    directory_gid = (
        expected_gid if expected_directory_gid is None else expected_directory_gid
    )
    try:
        directory_fd = os.open(str(path.parent), directory_flags)
    except OSError as exc:
        raise PolicyRefused(
            f"설정 디렉터리를 안전하게 열 수 없다 ({path.parent}): {exc}"
        ) from exc

    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise PolicyRefused(f"설정 디렉터리가 디렉터리가 아니다 ({path.parent})")
        if directory_stat.st_uid != directory_uid:
            raise PolicyRefused(
                f"설정 디렉터리가 root 소유가 아니다 "
                f"({path.parent}, uid={directory_stat.st_uid})"
            )
        if directory_gid is not None and directory_stat.st_gid != directory_gid:
            raise PolicyRefused(
                f"설정 디렉터리가 wheel 그룹이 아니다 "
                f"({path.parent}, gid={directory_stat.st_gid})"
            )
        directory_mode = stat.S_IMODE(directory_stat.st_mode)
        if directory_mode != 0o755:
            raise PolicyRefused(
                f"설정 디렉터리 권한이 0755가 아니다 "
                f"({path.parent}, mode={directory_mode:04o})"
            )

        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError as exc:
            raise PolicyRefused(
                f"정책이 없다 ({path}) — sudo install.sh 먼저"
            ) from exc
        except OSError as exc:
            raise PolicyRefused(f"정책을 안전하게 열 수 없다 ({path}): {exc}") from exc

        try:
            policy_stat = os.fstat(fd)
            if not stat.S_ISREG(policy_stat.st_mode):
                raise PolicyRefused(f"{path}가 일반 파일이 아니다")
            if policy_stat.st_uid != expected_uid:
                raise PolicyRefused(
                    f"{path}가 root 소유가 아니다 (uid={policy_stat.st_uid})"
                )
            if expected_gid is not None and policy_stat.st_gid != expected_gid:
                raise PolicyRefused(
                    f"{path}가 wheel 그룹이 아니다 (gid={policy_stat.st_gid})"
                )
            if policy_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise PolicyRefused(f"{path}에 group/other 쓰기 비트가 있다")
            policy_mode = stat.S_IMODE(policy_stat.st_mode)
            if policy_mode != 0o644:
                raise PolicyRefused(
                    f"{path} 권한이 0644가 아니다 (mode={policy_mode:04o})"
                )
            if policy_stat.st_nlink != 1:
                raise PolicyRefused(
                    f"{path}의 link count가 1이 아니다 (nlink={policy_stat.st_nlink})"
                )
            if policy_stat.st_size > MAX_POLICY_BYTES:
                raise PolicyRefused(f"{path}가 너무 크다 ({policy_stat.st_size}B)")

            chunks: list[bytes] = []
            remaining = MAX_POLICY_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_POLICY_BYTES:
                raise PolicyRefused(f"{path}가 읽는 동안 크기 상한을 넘었다")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)

    return _parse(raw, path)


def read(path: Path = POLICY_PATH) -> Policy:
    """Read the root:wheel policy without reopening a checked path (D2-3)."""
    return _read(Path(path), expected_uid=0, expected_gid=WHEEL_GID)


def read_staging(
    path: Path,
    *,
    expected_uid: int = os.getuid(),
) -> Policy:
    """Read caller-owned staging policy without treating inherited gid as identity."""
    return _read(
        Path(path),
        expected_uid=expected_uid,
    )


def bind(merged: "MergedModel", policy: Policy) -> "MergedModel":
    """Bind policy-owned identity to one merged model before validation/apply."""
    known_services = {service.name for service in merged.services}
    unknown_authorizations = sorted(set(policy.service_users) - known_services)
    if unknown_authorizations:
        raise PolicyRefused(
            "service_users가 존재하지 않는 서비스를 허용한다: "
            + ", ".join(repr(name) for name in unknown_authorizations)
        )

    services = []
    for service in merged.services:
        expected_label = resolve_label(service.name, policy)
        if expected_label is None:
            raise PolicyRefused(
                f"서비스 name이 policy label 규칙에 맞지 않는다: {service.name!r}"
            )
        if service.label != expected_label:
            raise PolicyRefused(
                f"서비스 label이 policy와 다르다 ({service.name}): "
                f"선언={service.label!r}, policy={expected_label!r}"
            )
        if service.group not in policy.groups:
            raise PolicyRefused(
                f"서비스 group이 policy에 없다 ({service.name}): {service.group!r}"
            )

        requested_user = service.user
        if requested_user is None:
            services.append(service)
            continue
        if (
            not isinstance(requested_user, str)
            or USER_PATTERN.fullmatch(requested_user) is None
        ):
            raise PolicyRefused(
                f"서비스 user가 잘못됐다 ({service.name}): {requested_user!r}"
            )
        allowed_user = policy.service_users.get(service.name)
        if allowed_user is None:
            raise PolicyRefused(
                f"서비스 user 허용이 policy.service_users에 없다 "
                f"({service.name}): 요청={requested_user!r}"
            )
        if requested_user != allowed_user:
            raise PolicyRefused(
                f"서비스 user가 policy.service_users와 다르다 ({service.name}): "
                f"요청={requested_user!r}, policy={allowed_user!r}"
            )
        services.append(replace(service, user=allowed_user))

    defaults = replace(merged.defaults, user=policy.service_user)
    return replace(merged, defaults=defaults, services=tuple(services))
