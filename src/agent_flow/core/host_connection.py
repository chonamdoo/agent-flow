"""host 연결 진단: 설치·등록·신뢰·실행을 따로 판정하고 지원 등급을 계산한다.

등급은 표시일 뿐이다. run 허용은 `hook_integrity.assert_managed_hooks_registered`가
정하고, 이 모듈은 그 판정을 바꾸지 않는다. 판정·강제 모듈은 이 모듈을 import하지
않는다.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from agent_flow.core.command_evidence import read_command_evidence
from agent_flow.core.hook_integrity import (
    HookInstallDescription,
    describe_managed_hooks,
    find_install_root,
)
from agent_flow.core.host_trust import HostTrust, TrustState, read_host_trust
from agent_flow.core.host_write_boundary import run_session_bindings

SupportLevel = Literal["hook_enforced", "hook_unproven", "runner_only"]
RegistrationState = Literal["ok", "missing", "unreadable", "incomplete"]
InstallState = Literal["ok", "not_found", "not_recorded", "hooks_disabled", "violations"]

HOOK_ENFORCED: Final = "hook_enforced"
HOOK_UNPROVEN: Final = "hook_unproven"
RUNNER_ONLY: Final = "runner_only"

HOSTS = ("claude", "codex", "omp")
# 등록 파일의 첫 경로 요소 → host. `.Codex`와 `.codex`는 같은 host다.
_SURFACE_HOSTS = {".claude": "claude", ".codex": "codex", ".omp": "omp"}

RUNNER_PROTECTIONS = (
    "필수 marker",
    "pause·승인 token",
    "독립 reviewer 2명 이상",
    "runner gate",
    "route 불변식",
    "run 시작 검사",
)
HOOK_PROTECTIONS = ("pre-block 2규칙", "tripwire", "session binding", "명령 기록")


@dataclass(frozen=True)
class HostRow:
    host: str
    registered: RegistrationState
    registration_violations: tuple[str, ...]
    trust: HostTrust
    level: SupportLevel


@dataclass(frozen=True)
class ExecutionObservation:
    bindings: int
    guidance_eligible: bool
    commands: int
    exit_codes_missing: int
    last_command_at: float | None

    @property
    def observed(self) -> bool:
        return self.bindings > 0 or self.commands > 0


@dataclass(frozen=True)
class HostConnectionReport:
    install_state: InstallState
    install_root: Path | None
    install_violations: tuple[str, ...]
    active_host: str | None
    detected_by: str
    active_level: SupportLevel
    hosts: tuple[HostRow, ...]
    execution: ExecutionObservation


def support_level(
    *,
    installed: bool,
    registered: bool,
    trust: TrustState,
    detected_by: str | None,
    executed: bool,
) -> SupportLevel:
    """`detected_by`는 이 host가 active host일 때만 값이 있다(`env:<NAME>` 또는 `path`).

    관측된 실행에는 host 표시가 없다. 그래서 env로 확인된 active host에만 귀속한다 —
    PATH에 있다는 것은 그 CLI가 이 세션의 host라는 증거가 아니다.
    """
    if not installed or not registered or trust == "no":
        return RUNNER_ONLY
    if executed and detected_by is not None and detected_by.startswith("env:"):
        return HOOK_ENFORCED
    return HOOK_UNPROVEN


def collect_host_connection(
    *,
    project_root: Path,
    checkout: Path,
    run_id: str,
    run_started_at: float | None,
    active: tuple[str | None, str],
    home: Path,
    env: Mapping[str, str],
) -> HostConnectionReport:
    """`run_started_at`이 없으면 실행 기록은 세지 않는다. 범위를 모르는 기록은 이번 run의 증거가 아니다."""
    active_host, detected_by = active
    install_root = find_install_root(checkout)
    description = (
        describe_managed_hooks(install_root, checkout) if install_root is not None else None
    )
    install_state = _install_state(description)
    execution = _execution(project_root, install_root, checkout, run_id, run_started_at)
    rows = []
    for host in HOSTS:
        registered, violations = _registration(host, description)
        trust = read_host_trust(
            host, home=home, checkout=checkout, project_root=project_root, env=env
        )
        rows.append(
            HostRow(
                host=host,
                registered=registered,
                registration_violations=violations,
                trust=trust,
                level=support_level(
                    installed=install_state == "ok",
                    registered=registered == "ok",
                    trust=trust.state,
                    detected_by=detected_by if host == active_host else None,
                    executed=execution.observed,
                ),
            )
        )
    return HostConnectionReport(
        install_state=install_state,
        install_root=install_root,
        install_violations=description.install_violations if description else (),
        active_host=active_host,
        detected_by=detected_by,
        active_level=next((row.level for row in rows if row.host == active_host), RUNNER_ONLY),
        hosts=tuple(rows),
        execution=execution,
    )


def render_host_connection(report: HostConnectionReport) -> list[str]:
    """사람용 줄과 `host_connection_json:` 한 줄.

    `status_json:`과 `run:`/`next_command:`로 시작하는 줄은 만들지 않는다. binding
    hook이 status 출력에서 그 줄을 읽는다.
    """
    lines = ["host_connection: hook 보호 수준 — 이 표시는 run을 막지 않습니다"]
    root = f" ({_printable(report.install_root)})" if report.install_root else ""
    lines.append(f"  install: {report.install_state}{root}")
    lines.extend(f"    - {_printable(violation)}" for violation in report.install_violations)
    lines.append(
        f"  active host: {report.active_host or 'none'} ({report.detected_by}) "
        f"level={report.active_level}"
    )
    for row in report.hosts:
        lines.append(
            f"  {row.host}: registered={row.registered} trusted={row.trust.state} "
            f"level={row.level}"
        )
        if row.trust.hint:
            lines.append(f"    hint: {row.trust.hint}")
    execution = report.execution
    lines.append(
        f"  execution: {'observed' if execution.observed else 'not observed'} "
        f"(bindings={execution.bindings}, "
        f"guidance={'yes' if execution.guidance_eligible else 'no'}, "
        f"commands={execution.commands}, exit code 없음={execution.exit_codes_missing})"
    )
    if execution.exit_codes_missing:
        lines.append(
            "    hint: exit code를 주지 않는 host에서는 실패한 명령을 볼 수 없어 "
            "RED 관측을 요구하지 못합니다"
        )
    if report.active_level == RUNNER_ONLY or any(row.level == RUNNER_ONLY for row in report.hosts):
        lines.append("  runner_only에서도 남는 보호: " + ", ".join(RUNNER_PROTECTIONS))
        lines.append("  runner_only에서 증명되지 않는 보호: " + ", ".join(HOOK_PROTECTIONS))
    lines.append(
        "host_connection_json: "
        + json.dumps(_payload(report), ensure_ascii=False, sort_keys=True)
    )
    return lines


def _install_state(description: HookInstallDescription | None) -> InstallState:
    if description is None:
        return "not_found"
    if description.enabled is None:
        return "not_recorded"
    if not description.enabled:
        return "hooks_disabled"
    return "violations" if description.install_violations else "ok"


def _registration(
    host: str, description: HookInstallDescription | None
) -> tuple[RegistrationState, tuple[str, ...]]:
    if description is None:
        return "missing", ()
    surfaces = [
        surface
        for surface in description.surfaces
        if _SURFACE_HOSTS.get(Path(surface.name).parts[0].lower()) == host
    ]
    violations = tuple(
        dict.fromkeys(violation for surface in surfaces for violation in surface.violations)
    )
    if not any(surface.present for surface in surfaces):
        return "missing", violations
    if any(surface.present and not surface.readable for surface in surfaces):
        return "unreadable", violations
    return ("incomplete" if violations else "ok"), violations


def _execution(
    project_root: Path,
    install_root: Path | None,
    checkout: Path,
    run_id: str,
    since: float | None,
) -> ExecutionObservation:
    checkout_real = Path(os.path.realpath(checkout))
    bindings = [
        binding
        for binding in run_session_bindings(project_root, run_id)
        if Path(os.path.realpath(binding.checkout.checkout)) == checkout_real
    ]
    runs = (
        read_command_evidence(install_root, since=since, cwd_root=checkout_real).runs
        if install_root is not None and since is not None
        else ()
    )
    return ExecutionObservation(
        bindings=len(bindings),
        guidance_eligible=any(binding.guidance_eligible for binding in bindings),
        commands=len(runs),
        exit_codes_missing=sum(run.exit_code is None for run in runs),
        last_command_at=max((run.at for run in runs), default=None),
    )


def _printable(value: object) -> str:
    """디스크의 파일 이름은 줄바꿈을 품을 수 있다. 그대로 찍으면 `status_json:` 줄을 위조한다."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in str(value))


def _utc_iso(timestamp: float | None) -> str | None:
    """기록의 `at`은 검증되지 않은 숫자다. 날짜로 바꿀 수 없는 값은 없는 것으로 낸다."""
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def _payload(report: HostConnectionReport) -> dict:
    execution = report.execution
    return {
        "blocks_run": False,
        "install": {
            "state": report.install_state,
            "root": str(report.install_root) if report.install_root else None,
            "violations": list(report.install_violations),
        },
        "active_host": {
            "name": report.active_host,
            "detected_by": report.detected_by,
            "level": report.active_level,
        },
        "hosts": {
            row.host: {
                "registered": row.registered,
                "registration_violations": list(row.registration_violations),
                "trusted": row.trust.state,
                "trust_source": row.trust.source,
                "trust_detail": row.trust.detail,
                "hint": row.trust.hint,
                "level": row.level,
            }
            for row in report.hosts
        },
        "execution": {
            "observed": execution.observed,
            "bindings": execution.bindings,
            "guidance_eligible": execution.guidance_eligible,
            "commands": execution.commands,
            "exit_codes_missing": execution.exit_codes_missing,
            "last_command_at": _utc_iso(execution.last_command_at),
        },
    }
