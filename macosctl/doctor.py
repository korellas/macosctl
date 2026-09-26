"""3자 대조 드리프트 진단 — 선언(매니페스트) vs 등록(plist/launchd) vs 실제(포트/프로세스).

설계 근거: docs/design.md

`diagnose()`는 순수 함수다. 입력은 SystemState 하나이고 라이브든 픽스처든 같은
모양이다.

드리프트 Finding은 정상 상태에서 조용해야 한다. D8의 오버라이드 현황은 예외로,
정상 정보 섹션이므로 항상 표시하되 exit code에는 영향을 주지 않는다. 지울 수 없는
enabled 잔재나 managed=false의 정상 다운은 드리프트로 보고하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from macosctl import inventory
from macosctl import model
from macosctl.manifest import Service
from macosctl.collect import SystemState


# 우리 네임스페이스. 오버라이드 잔재를 볼 때 이 밖은 쳐다보지 않는다 — 시스템에는
# Apple이 정당하게 disabled 시켜 둔 데몬(com.apple.ftpd, bootpd 등)이 늘 있고,
# 그걸 우리 드리프트로 올리면 doctor 첫 실행부터 오탐으로 시작한다.
#
# 주의: 이 접두사는 **범위를 좁히는 필터**일 뿐 소유권 증명이 아니다. 무엇을
# 은퇴시킬지 판정하는 것은 인벤토리다 (설계 스펙 D4) — 접두사로 은퇴를 판정하면
# com.korellas.log-rotate 같은 매니페스트 밖 정당 설치물을 죽인다.
OUR_LABEL_PREFIXES = ("com.korellas.", "com.webtop")
VOLUMES_ROOT = Path("/Volumes")


@dataclass(frozen=True)
class Finding:
    code: str
    service: str
    detail: str
    action: str


@dataclass(frozen=True)
class Override:
    service: str
    field: str
    winner: str
    overridden: tuple[str, ...]


def _is_ours(label: str) -> bool:
    return label.startswith(OUR_LABEL_PREFIXES)


def diagnose(
    services: tuple[Service, ...], state: SystemState
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    for svc in services:
        findings.extend(_diagnose_service(svc, state))

    findings.extend(_diagnose_overrides(services, state))
    return tuple(findings)


def diagnose_merged(
    merged: model.MergedModel,
    state: SystemState,
    known_inventory: dict[str, inventory.Entry] | None = None,
) -> tuple[Finding, ...]:
    """Diagnose the immutable merged model plus persistent inventory state."""
    findings = list(diagnose(merged.services, state))
    findings.extend(
        _external_volume_findings(merged.services, merged.defaults.log_dir)
    )

    # confd owns orphan detection while it walks directories. Convert its one
    # stable warning form here so the warning becomes a standing drift finding.
    for warning in merged.warnings:
        orphan = model.parse_orphan_dropin_warning(warning)
        if orphan is None:
            continue
        name, directory = orphan
        findings.append(Finding(
            "orphan-dropin",
            name,
            f"대상 서비스 없이 드롭인 디렉터리가 남아 있다: {directory}",
            "서비스 조각을 다시 link하거나 드롭인 디렉터리를 검토 후 정리할 것",
        ))

    services_by_label = {service.label: service for service in merged.services}
    masked_labels = {
        service.label for service in merged.services
        if service.lifecycle == "masked"
    }
    if known_inventory is not None:
        masked_labels.update(
            label for label, entry in known_inventory.items()
            if entry.lifecycle == "masked"
        )
    for label in sorted(masked_labels):
        plist_present = label in state.installed_labels
        job = state.jobs.get(label)
        job_present = bool(job and job.loaded)
        if not (plist_present or job_present):
            continue
        service = services_by_label.get(label)
        name = service.name if service is not None else label
        present = ", ".join(
            item for item, condition in (
                ("plist", plist_present), ("launchd job", job_present)
            ) if condition
        )
        findings.append(Finding(
            "masked-but-installed",
            name,
            f"masked 서비스인데 {present}이 남아 있다 ({label})",
            "다른 설치 절차가 같은 라벨을 쓰지 않는지 확인한 뒤 sudo macosctl apply",
        ))

    return tuple(findings)


def _external_volume_findings(
    services: tuple[model.MergedService, ...],
    default_log_dir: str,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    for service in services:
        if not service.managed:
            continue

        references: list[tuple[str, Path]] = []
        declared = [
            ("working_directory", service.working_directory),
            ("log_dir", service.log_dir or default_log_dir),
            *(
                (f"exec[{index}]", value)
                for index, value in enumerate(service.exec_argv)
            ),
            *((f"env.{key}", value) for key, value in service.env),
        ]
        for field, value in declared:
            if not isinstance(value, str):
                continue
            path = Path(value)
            if not path.is_absolute():
                continue
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError):
                resolved = path
            if resolved != VOLUMES_ROOT and VOLUMES_ROOT not in resolved.parents:
                continue
            reference = (field, resolved)
            if reference not in references:
                references.append(reference)

        if not references:
            continue

        rendered = ", ".join(
            f"{field} → {resolved}" for field, resolved in references
        )
        findings.append(Finding(
            "external-volume-tcc-risk",
            service.name,
            f"LaunchDaemon 선언 경로가 /Volumes 아래를 참조한다: {rendered}. "
            f"이 마운트가 외장/removable volume이면 macOS TCC가 background session의 "
            f"접근을 거부할 수 있고, Unix 파일 권한이 정상이어도 실패하며 KeepAlive "
            f"재기동이 반복될 수 있다. macosctl은 볼륨 유형이나 현재 TCC 승인 상태를 "
            f"판정하지 않는다",
            "실제 실행 클라이언트에 시스템 설정에서 해당 볼륨 유형의 접근을 승인할 "
            "것. background LaunchDaemon에서 승인할 수 없다면 내부 디스크 경로를 "
            "쓰거나 사용자 세션 LaunchAgent/foreground supervisor로 운용할 것",
        ))

    return tuple(findings)


def override_summary(merged: model.MergedModel) -> tuple[Override, ...]:
    """Return normal (non-drift) overrides and each field's winning file."""
    rows: list[Override] = []
    for service in merged.services:
        by_field: dict[str, list[str]] = {}
        for provenance in service.sources:
            location = provenance.location or provenance.source
            sources = by_field.setdefault(provenance.field, [])
            if not sources or sources[-1] != location:
                sources.append(location)
        for field, sources in by_field.items():
            distinct = tuple(dict.fromkeys(sources))
            if len(distinct) < 2:
                continue
            rows.append(Override(
                service.name, field, sources[-1], tuple(sources[:-1])
            ))
    return tuple(rows)


def _diagnose_service(svc: Service, state: SystemState) -> list[Finding]:
    """서비스 하나의 드리프트. **조치는 항상 정확히 하나**여야 한다.

    "apply / start" 같은 복수 대안은 조치가 아니다 — 어느 쪽이 맞는지 아는 것이
    doctor의 일이고, 그 판단을 사람에게 되던지면 진단이 아무것도 좁히지 못한다.
    """
    findings: list[Finding] = []
    job = state.jobs.get(svc.label)
    listeners = state.listeners_on(svc.port)
    installed = svc.label in state.installed_labels
    stopped = svc.label in state.stopped_this_boot

    # ① 선언됐는데 등록이 없다 (managed 한정 — managed=false는 은퇴가 정상이다)
    if svc.managed and not installed:
        findings.append(
            Finding(
                "plist-missing",
                svc.name,
                f"매니페스트에 있으나 {svc.label}.plist가 설치돼 있지 않다",
                "sudo macosctl apply",
            )
        )

    job_pid = job.pid if job else None
    owned = tuple(l for l in listeners if job_pid and state.is_descendant(l.pid, job_pid))
    foreign = tuple(l for l in listeners if l not in owned)

    # ④⑤ 포트를 쥔 프로세스가 launchd job 트리 밖이다.
    #
    # 한 포트를 두 프로세스가 동시에 바인드할 수는 없으므로, 판정을 가르는 것은
    # "포트에 누가 더 있는가"가 아니라 **launchd job이 살아 있는가**다.
    #
    #   · job 없음      → rogue-listener      (순수 우회 기동)
    #   · job 살아 있음 → duplicate-instance  (인스턴스가 둘, 그중 launchd 것이
    #                                          서빙하지 못하고 있다)
    if foreign and not owned:
        job_alive = bool(job and job.loaded and job.pid)
        if not svc.managed:
            if getattr(svc, "lifecycle", "active") != "masked":
                findings.append(
                    Finding(
                        "unmanaged-listener",
                        svc.name,
                        f"managed=false인데 :{svc.port}에 프로세스가 떠 있다 "
                        f"(pid {', '.join(str(l.pid) for l in foreign)}) — 수동 기동으로 보인다",
                        "정상 운영이면 조치 불요",
                    )
                )
        elif job_alive:
            findings.append(
                Finding(
                    "duplicate-instance",
                    svc.name,
                    f"launchd job(pid {job.pid})은 살아 있으나 :{svc.port}은 관리 밖 "
                    f"프로세스({', '.join(str(l.pid) for l in foreign)})가 쥐고 있다 — "
                    f"launchd 인스턴스가 서빙하지 못하는 상태다",
                    f"관리 밖 프로세스를 내린 뒤 macosctl restart {svc.name}",
                )
            )
        else:
            findings.append(
                Finding(
                    "rogue-listener",
                    svc.name,
                    f":{svc.port}을 관리 밖 프로세스가 쥐고 있다 "
                    f"(pid {', '.join(str(l.pid) for l in foreign)})",
                    f"관리 밖 프로세스를 내린 뒤 macosctl start {svc.name}",
                )
            )

    # ③ managed인데 아무도 서빙하지 않는다. 왜 안 뜨는지가 조치를 정한다.
    if svc.managed and installed and not listeners:
        # 실제 boot policy의 정본은 launchd 투영값이다 — provenance가 아니다.
        boot_disabled = state.disabled_overrides.get(svc.label, False)
        finding = _why_not_serving(svc, job, job_pid, stopped, boot_disabled)
        if finding is not None:
            findings.append(finding)

    # ⑦ 이번 부팅의 정지 의도가 남았는데 실제로는 떠 있다.
    #
    # 다른 부팅의 의도는 boot session UUID 불일치로 이미 만료돼 여기까지 오지
    # 않는다 — 재부팅이 RunAtLoad로 되살린 서비스에 대고 "정지시켰다"고 말하던
    # 오탐이 그 경로였다.
    if stopped and (listeners or job_pid is not None):
        findings.append(
            Finding(
                "stale-stop-mark",
                svc.name,
                "이번 부팅의 정지 의도가 남아 있는데 실제로는 실행 중이다",
                f"macosctl start {svc.name}",
            )
        )

    return findings


def _why_not_serving(
    svc: Service, job, job_pid: int | None, stopped: bool, boot_disabled: bool
) -> Finding | None:
    """리스너가 없을 때, 어느 계층에서 끊겼는지로 단일 조치를 고른다.

    None은 "runtime 계층에는 할 말이 없다"는 뜻이다 — boot policy 축의 판정은
    _diagnose_overrides가 실제 정책과 provenance를 대조해 따로 내린다.
    """
    if stopped:
        # 이번 부팅의 정지 의도가 boot policy보다 구체적이다. 두 축이 모두 내려가
        # 있어도 운영자가 마지막으로 표명한 쪽을 조치로 삼는다.
        return Finding(
            "stopped-by-operator",
            svc.name,
            "이번 부팅에서 의도적으로 정지시킨 상태다",
            f"macosctl start {svc.name}",
        )
    if job is None or not job.loaded:
        if boot_disabled:
            # inactive + boot-disabled는 도달 가능한 정상 상태다 (disable은 runtime을
            # 보존한다). 여기서 start를 권하면 start가 boot policy를 그대로 두므로
            # 다음 진단에 같은 경고가 다시 뜬다 — 해소되지 않는 조치다.
            return None
        return Finding(
            "not-running",
            svc.name,
            f"plist는 설치돼 있으나 launchd에 job이 없다 (:{svc.port} 리스너 없음)",
            f"macosctl start {svc.name}",
        )
    if job_pid is None:
        # loaded-but-no-PID는 kill이나 kickstart로 낫지 않는다. 등록 자체를 다시
        # 세워야 한다 — webtop 장애에서 PID 기반 재기동만 반복하다 못 고친 상태다.
        return Finding(
            "loaded-without-pid",
            svc.name,
            f"launchd에 job은 있으나 PID가 없다 (state={job.state}) — "
            f"재등록 없이는 복구되지 않는다",
            f"macosctl restart {svc.name} --recreate",
        )
    return Finding(
        "no-owned-listener",
        svc.name,
        f"PID {job_pid}는 살아 있으나 :{svc.port}에 리스너가 없다 — "
        f"프로세스가 기동 중이거나 바인드에 실패한 상태다",
        f"macosctl logs {svc.name} 확인 후 macosctl restart {svc.name} --wait",
    )


def _diagnose_overrides(
    services: tuple[Service, ...], state: SystemState
) -> list[Finding]:
    """**boot policy**의 실제와 macosctl의 조작 출처를 대조한다.

    실제 정본은 launchd(`print-disabled`)이고, state의 provenance는 "macosctl이
    그렇게 만들었다"는 출처일 뿐이다. 둘은 같은 사실의 중복 정본이 아니라 서로
    다른 축이므로, 어긋남에도 세 가지 다른 의미가 있다:

      · 실제 disabled + 출처 있음  → 의도한 상태다. 침묵한다.
      · 실제 disabled + 출처 없음  → 아무도 의도하지 않았다. 되돌린다.
      · 실제 enabled  + 출처 있음  → 출처가 낡았다. 다시 맞춘다.

    launchctl에는 오버라이드 **항목**을 삭제하는 verb가 없다 — enable/disable은 값
    토글일 뿐이다. 따라서 `enabled` 잔재 자체는 지울 방법이 없는 무해한 찌꺼기이고,
    이걸 보고하면 영원히 해소되지 않는 경고가 된다.
    """
    declared = {s.label: s for s in services}
    findings: list[Finding] = []
    labels = sorted(
        set(state.disabled_overrides) | set(state.disabled_by_macosctl)
    )

    for label in labels:
        if not _is_ours(label):
            continue
        disabled = state.disabled_overrides.get(label, False)
        provenance = label in state.disabled_by_macosctl
        svc = declared.get(label)

        if disabled and provenance:
            continue  # 의도한 상태다

        if disabled and svc is None:
            findings.append(Finding(
                "stale-disabled-override",
                label,
                f"매니페스트에 없는 {label}이 disabled로 남아 있다",
                "이 라벨을 다시 쓰지 않는다면 무해하다. 재사용하려면 매니페스트에 "
                "선언하고 `sudo macosctl apply` — 등록이 enable을 선행하므로 해소된다",
            ))
        elif disabled and svc.managed:
            findings.append(Finding(
                "declared-but-disabled",
                label,
                f"{svc.name}은 managed로 선언됐는데 아무도 의도하지 않은 disabled "
                f"오버라이드가 남아 있다 — 재부팅해도 뜨지 않는다",
                f"macosctl enable {svc.name}",
            ))
        elif provenance and not disabled:
            name = svc.name if svc is not None else label
            findings.append(Finding(
                "stale-disable-provenance",
                label,
                f"state에는 macosctl이 {label}을 disable했다고 남아 있는데 launchd는 "
                f"enabled다 — 조작 출처가 실제와 어긋난다",
                f"macosctl enable {name}",
            ))

    return findings


def exit_code(findings: tuple[Finding, ...]) -> int:
    """0=정상, 1=드리프트 있음. 훅/CI에서 재사용할 수 있게 한다."""
    return 1 if findings else 0
