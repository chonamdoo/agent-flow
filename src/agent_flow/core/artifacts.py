from __future__ import annotations

import json
from pathlib import Path

from agent_flow.core.atomic_io import atomic_write_text
from agent_flow.core.gates import GateResult, gate_results_timed_out, relativize_local_paths
from agent_flow.core.report import write_run_report
from agent_flow.core.security import validate_safe_name
from agent_flow.core.worktree_isolation import AGENT_FLOW_STATE_DIRS, write_run_subpath_text


def init_project(root: Path) -> None:
    # tripwire가 비교에서 빼는 목록과 **같은 소스**여야 한다. 갈라지면 정상
    # 명령이 leader 오염으로 오탐된다.
    for name in AGENT_FLOW_STATE_DIRS:
        (root / ".agent-flow" / name).mkdir(parents=True, exist_ok=True)


def write_gate_results(
    *,
    run_dir: Path,
    results: list[GateResult],
    cwd: Path | None = None,
    phase: str = "",
) -> Path:
    # timeout은 "optional 실패"가 아니라 "판정 불가"다. required만 세면 검증이
    # 끊긴 실행이 green으로 기록되고, 그 상태를 읽는 shell/CI가 성공으로 본다.
    timed_out = gate_results_timed_out(results)
    passed = not timed_out and all(
        result.passed or not result.required for result in results
    )
    # gate 출력에 실린 절대 경로는 이 파일이 쓰이는 순간 artifact가 된다.
    # command와 같은 규칙으로 상대화하지 않으면 로컬 경로가 그대로 남는다.
    base = cwd if cwd is not None else _checkout_root(run_dir)
    serialized_results = [_gate_result_payload(result, base) for result in results]
    payload = {
        "passed": passed,
        "status": "error" if timed_out else "green" if passed else "request-changes",
        "results": serialized_results,
    }
    # 출처 표식. runner는 이 값이 run meta의 nonce와 같을 때만 green으로 라우팅한다.
    # 이 층이 막는 것은 손으로 쓴 gate-results.json이지 적대적 위조가 아니다 —
    # nonce도 디스크에 있으므로 읽어서 복사할 수 있다. 진짜 해법은 runner가
    # `run_gates`를 직접 부르는 것이고, 그때까지의 임시방편이다.
    nonce = run_gate_nonce(run_dir)
    if nonce:
        # phase도 함께 남긴다. 어떤 gate가 돌았는지는 결과 목록으로 알 수 있지만
        # "무엇을 돌리려 했는가"는 필터 값에만 있다. build/test는 pre-push라
        # `--phase pre-commit`으로는 애초에 목록에 오르지 않으므로, 이 값이 없으면
        # runner는 "안 돈 것"과 "없어서 안 돈 것"을 구분할 수 없다.
        payload["produced_by"] = {
            "tool": "agent-flow gates",
            "nonce": nonce,
            "gate_phase": phase,
        }
    path = run_dir / "artifacts" / "gate-results.json"
    write_run_subpath_text(run_dir, path, f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
    legacy_path = run_dir / "gate-results.json"
    write_run_subpath_text(
        run_dir, legacy_path, f"{json.dumps(serialized_results, indent=2, sort_keys=True)}\n"
    )
    write_run_report(run_dir)
    return path


def _checkout_root(run_dir: Path) -> Path:
    """``run_dir``을 담고 있는 체크아웃 루트.

    gate는 이 자리를 cwd로 돌았으므로 출력의 절대 경로도 같은 기준으로 상대화해야
    command 정규화와 어긋나지 않는다. ``.agent-flow`` 표식이 없으면(임시 run_dir)
    run_dir 자신을 기준으로 둔다 — 기준을 못 찾았다고 정규화를 건너뛰면 절대
    경로가 그대로 남는다.
    """
    resolved = run_dir.resolve()
    for parent in resolved.parents:
        if parent.name == ".agent-flow":
            return parent.parent
    return resolved


def _gate_result_payload(result: GateResult, base: Path) -> dict[str, object]:
    return {
        "gate_id": result.gate_id,
        "command": " ".join(result.command),
        "argv": list(result.command),
        "passed": result.passed,
        "required": result.required,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": relativize_local_paths(result.stdout, base),
        "stderr": relativize_local_paths(result.stderr, base),
    }


def write_handoff(
    *,
    root: Path,
    run_dir: Path,
    from_stage: str,
    to_stage: str,
    decided: str,
    rejected: str,
    risks: str,
    files: str,
    remaining: str,
) -> Path:
    init_project(root)
    # 이 이름은 run 안팎 두 경로의 마지막 컴포넌트가 된다. CLI가 준 값을 그대로
    # 쓰면 `--from-stage ../../../../tmp/x`가 `mkdir -p`를 타고 run 밖에 파일을
    # 만든다. 경로 컴포넌트가 될 값은 여기서 이름으로 증명하고 들어간다.
    from_stage = validate_safe_name(from_stage, "handoff stage")
    to_stage = validate_safe_name(to_stage, "handoff stage")
    filename = f"{from_stage}-to-{to_stage}.md"
    content = "\n".join(
        [
            f"# Handoff: {from_stage} -> {to_stage}",
            "",
            f"- Decided: {decided or 'None'}",
            f"- Rejected: {rejected or 'None'}",
            f"- Risks: {risks or 'None'}",
            f"- Files: {files or 'None'}",
            f"- Remaining: {remaining or 'None'}",
            "",
        ]
    )
    run_path = run_dir / "handoffs" / filename
    write_run_subpath_text(run_dir, run_path, content)
    write_run_report(run_dir)

    project_path = root / ".agent-flow" / "handoffs" / filename
    project_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(project_path, content)
    return run_path


def write_recovery(
    *,
    run_dir: Path,
    title: str,
    cause: str,
    artifacts: list[str],
    rerun_command: str,
    manual_action: str,
) -> Path:
    path = run_dir / "recovery.md"
    lines = [
        f"# Recovery: {title}",
        "",
        f"- Cause: {cause or 'Unknown'}",
        f"- Rerun Command: {rerun_command or 'None'}",
        f"- Manual Action: {manual_action or 'None'}",
        "",
        "## Artifacts",
        "",
    ]
    lines.extend(f"- {artifact}" for artifact in artifacts) if artifacts else lines.append("- None")
    lines.append("")
    write_run_subpath_text(run_dir, path, "\n".join(lines))
    write_run_report(run_dir)
    return path


def run_gate_nonce(run_dir: Path) -> str:
    """run meta에 심긴 gate nonce. 없으면 빈 문자열(구버전 run이나 직접 호출).

    Python runner는 `meta.json`에, Node runner는 `manifest.json`에 쓴다. 한쪽만
    보면 다른 쪽 runner가 연 run에서 gates 산출물이 출처 없이 남고, 그 run은
    green으로 라우팅되지 못한다.
    """
    for name in ("meta.json", "manifest.json"):
        try:
            payload = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nonce = payload.get("gate_nonce") if isinstance(payload, dict) else None
        if isinstance(nonce, str) and nonce:
            return nonce
    return ""
