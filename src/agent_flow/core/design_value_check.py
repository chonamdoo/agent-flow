"""수치 대조 — 원장의 값이 실제 diff에 나타나는지 본다.

관측자는 **git**이다. 원장은 agent가 쓰지만 diff는 아니므로, "16dp라고 적어
놓고 12dp를 썼다"가 여기서 처음 기계적으로 보인다.

**이 층의 한계 두 가지를 먼저 적는다.**

1. 원장 자체가 agent의 판독이다. 판독이 틀렸으면 이 gate는 "항상 같게 틀림"을
   보장할 뿐이다. 그래서 이 검사는 원장 확인(V3)이 있는 상태에서만 의미가 있다.
2. `Spacing.m`(=16dp)처럼 토큰을 경유한 코드는 grep으로 안 보인다. 그 경우를
   위반으로 들면 정상 구현이 fix-loop에 갇힌다. 그래서 토큰 경유는 금지가 아니라
   **명시**를 요구한다 — `design-values-implemented: <key>=<token>`으로 이름을
   대고, 그 이름이 diff에 실제로 있어야 인정된다. 이름을 대는 것은 자기신고지만
   **이름이 diff에 있는지는 여전히 git이 판정한다.**
"""
from __future__ import annotations

import re
import shlex
import stat
from pathlib import Path
from typing import Sequence

from agent_flow.core.commands import run_safe_command
from agent_flow.core.command_evidence import (
    is_test_command_execution,
    read_command_evidence,
    resolve_test_command_tokens,
)
from agent_flow.core.design_ledger import (
    LEDGER_SOURCE_PHASES,
    parse_spec_item_section,
    read_manual_spec_approvals,
    read_ledger,
)
from agent_flow.core.markers import completion_gate_marker_values
from agent_flow.core.phase_workflow import overall_review_route_key
from agent_flow.core.worktree_isolation import git_repo_state

# 구현을 판정하는 phase. 여기서 안 잡으면 gates는 build/test만 보고 통과시킨다.
DESIGN_VALUE_PHASES = frozenset({"final-review", "multi-review"})

IMPLEMENTED_MARKER = "design-values-implemented:"
_FALLBACK_BASES = ("main", "master")
_GIT_TIMEOUT_S = 60
_UNTRACKED_READ_LIMIT_BYTES = 1024 * 1024


def missing_spec_item_evidence(
    project_root: Path,
    run_dir: Path,
    phase_id: str,
    text: str,
    *,
    task_text: str = "",
    profile: dict | None = None,
    since: float | None = None,
    evidence_root: Path | None = None,
) -> list[str]:
    if phase_id in LEDGER_SOURCE_PHASES:
        parsed = parse_spec_item_section(text)
        missing = list(parsed.errors)
        declared_raw = completion_gate_marker_values(text).get("spec-items", "").strip()
        declared = declared_raw.lower()
        if parsed.items and declared in {"none", "n/a", "na", "-"}:
            ids = ", ".join(item.spec_id for item in parsed.items)
            missing.append(
                f"spec-items: {ids} (the section records SPEC items; 'none' contradicts it)"
            )
        elif parsed.items and not parsed.errors and declared_raw:
            expected_ids = tuple(item.spec_id for item in parsed.items)
            declared_ids = tuple(
                part.strip().upper()
                for part in declared_raw.split(",")
                if part.strip()
            )
            if declared_ids != expected_ids:
                missing.append(
                    f"spec-items: expected {', '.join(expected_ids)} "
                    f"(declared {', '.join(declared_ids)})"
                )
        if not parsed.items and not parsed.errors and task_text.strip():
            missing.append(
                "spec-items: at least one SPEC item "
                "(the run task contains user instructions; 'none' is not allowed)"
            )
        return missing
    if phase_id not in DESIGN_VALUE_PHASES:
        return []
    if overall_review_route_key(text) == "request-changes":
        return []
    ledger = read_ledger(run_dir)
    if not ledger.exists:
        return ["spec-ledger: design-spec.md is missing"]
    if ledger.errors:
        return [f"spec-ledger: {error}" for error in ledger.errors]
    if task_text.strip() and not ledger.spec_items:
        return ["spec-ledger: no SPEC items for a non-empty task"]
    evidence = read_command_evidence(
        evidence_root or project_root,
        since=since,
        cwd_root=project_root,
    )
    changed = (
        changed_file_evidence(project_root, profile=profile)
        if any(item.verification.lower().startswith("symbol:") for item in ledger.spec_items)
        else {}
    )
    manual_approvals = read_manual_spec_approvals(run_dir)
    test_commands = resolve_test_command_tokens(profile)
    unmet: list[str] = []
    for item in ledger.spec_items:
        lowered = item.verification.lower()
        if lowered.startswith("test:"):
            test_name = item.verification.partition(":")[2].strip()
            matching_runs = [
                run
                for run in evidence.runs
                if any(
                    is_test_command_execution(run.command, tokens, test_name)
                    for tokens in test_commands
                )
            ]
            latest = max(matching_runs, key=lambda run: run.at, default=None)
            passed = latest is not None and latest.exit_code == 0
            if not passed:
                unmet.append(
                    f"{item.spec_id}: test:{test_name} "
                    "(no passing observed test command includes the test name)"
                )
            continue
        if lowered.startswith("symbol:"):
            symbol, _, expected = item.verification.partition(":")[2].partition("=")
            symbol = symbol.strip()
            expected = expected.strip()
            passed = any(
                _appears(symbol, content) and _appears(expected, added)
                for content, added in (changed or {}).values()
            )
            if not passed:
                unmet.append(
                    f"{item.spec_id}: symbol:{symbol}={expected} "
                    "(value is not added in a changed file containing the symbol)"
                )
            continue
        if lowered == "manual" and item.spec_id not in manual_approvals:
            unmet.append(f"{item.spec_id}: manual (no user approval record)")
    return unmet



def missing_design_value_implementations(
    project_root: Path,
    run_dir: Path,
    phase_id: str,
    text: str,
    *,
    profile: dict | None = None,
) -> list[str]:
    if phase_id not in DESIGN_VALUE_PHASES:
        return []
    ledger = read_ledger(run_dir)
    if not ledger.values:
        return []
    if git_repo_state(project_root) != "repo":
        # git이 없으면 대조할 관측자가 없다. 관측 불가를 위반으로 들지 않는다.
        return []
    added = added_lines(project_root, profile=profile)
    if added is None:
        return []
    tokens = declared_tokens(text)
    unmet: list[str] = []
    for key, value in ledger.values:
        if _appears(value, added):
            continue
        token = tokens.get(key.lower())
        if token and _appears(token, added):
            continue
        if token:
            unmet.append(f"{key}={token} (declared token is not in the diff)")
        else:
            unmet.append(f"{key}={value}")
    if not unmet:
        return []
    return [
        f"design-values-implemented: {', '.join(unmet)} (recorded in design-spec.md "
        "but absent from the diff; use `<key>=<token>` when the value goes through a token)"
    ]


def declared_tokens(text: str) -> dict[str, str]:
    """`design-values-implemented: key=Token, other=Other` 를 맵으로 만든다.

    Completion Gate 파서는 줄을 소문자로 눕히는데, 여기서는 식별자 대소문자가
    곧 diff에서 찾을 문자열이라 원문에서 직접 읽는다.
    """
    tokens: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip().lstrip("+-*").strip()
        if not stripped.lower().startswith(IMPLEMENTED_MARKER):
            continue
        for part in stripped[len(IMPLEMENTED_MARKER):].split(","):
            key, separator, token = part.partition("=")
            if separator != "=":
                continue
            key = key.strip().lower()
            token = token.strip()
            if key and token:
                tokens[key] = token
    return tokens


def added_lines(project_root: Path, *, profile: dict | None = None) -> str | None:
    """이 작업이 **추가한** 줄만 모은다. 기존 코드에 이미 있던 값은 증거가 아니다.

    `git diff`는 untracked 파일을 안 보여준다. 새 파일이 통째로 안 보이면 새
    화면을 통째로 추가한 작업이 "수치를 하나도 안 썼다"로 잡힌다. 인덱스를
    건드리지 않기 위해 `add -N` 대신 untracked 파일을 직접 읽는다.
    """
    base = _merge_base(project_root, profile=profile)
    args = (
        ["git", "diff", "--unified=0", base]
        if base
        else ["git", "diff", "--unified=0", "HEAD"]
    )
    result = run_safe_command(args, cwd=project_root, timeout_s=_GIT_TIMEOUT_S)
    if not result.ok:
        return None
    chunks = [
        line
        for lines in _diff_added_lines(result.stdout).values()
        for line in lines
    ]
    chunks.extend(_untracked_text(project_root))
    return "\n".join(chunks)


def changed_file_evidence(
    project_root: Path,
    *,
    profile: dict | None = None,
) -> dict[str, tuple[str, str]] | None:
    base = _merge_base(project_root, profile=profile)
    args = (
        ["git", "diff", "--unified=0", base]
        if base
        else ["git", "diff", "--unified=0", "HEAD"]
    )
    result = run_safe_command(args, cwd=project_root, timeout_s=_GIT_TIMEOUT_S)
    if not result.ok:
        return None
    additions = _diff_added_lines(result.stdout)
    for relative, text in _untracked_files(project_root):
        additions[relative] = text.splitlines()
    evidence: dict[str, tuple[str, str]] = {}
    for relative, lines in additions.items():
        content = _read_regular_checkout_file(project_root, relative)
        if content is None:
            continue
        evidence[relative] = (content, "\n".join(lines))
    return evidence


def _diff_added_lines(diff_text: str) -> dict[str, list[str]]:
    additions: dict[str, list[str]] = {}
    current = ""
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current = ""
            in_hunk = False
            continue
        if not in_hunk and line.startswith("+++ "):
            try:
                target = shlex.split(line[4:])[0]
            except (ValueError, IndexError):
                target = ""
            current = target[2:] if target.startswith("b/") else ""
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and current and line.startswith("+"):
            additions.setdefault(current, []).append(line[1:])
    return additions


def _untracked_text(project_root: Path) -> list[str]:
    return [text for _, text in _untracked_files(project_root)]


def _untracked_files(project_root: Path) -> list[tuple[str, str]]:
    result = run_safe_command(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_root,
        timeout_s=_GIT_TIMEOUT_S,
    )
    if not result.ok:
        return []
    files: list[tuple[str, str]] = []
    for relative in result.stdout.split("\0"):
        if not relative:
            continue
        text = _read_regular_checkout_file(project_root, relative)
        if text is not None:
            files.append((relative, text))
            continue
    return files


def _read_regular_checkout_file(project_root: Path, relative: str) -> str | None:
    try:
        root = project_root.resolve()
        path = root / relative
        path.resolve().relative_to(root)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _UNTRACKED_READ_LIMIT_BYTES
        ):
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None




def _merge_base(project_root: Path, *, profile: dict | None) -> str | None:
    for candidate in _base_candidates(profile):
        result = run_safe_command(
            ["git", "merge-base", "HEAD", candidate], cwd=project_root, timeout_s=_GIT_TIMEOUT_S
        )
        if result.ok and result.stdout.strip():
            return result.stdout.strip()
    return None


def _base_candidates(profile: dict | None) -> Sequence[str]:
    branching = (profile or {}).get("branching")
    declared = branching.get("base") if isinstance(branching, dict) else None
    candidates = [declared] if isinstance(declared, str) and declared else []
    candidates.extend(_FALLBACK_BASES)
    return tuple(dict.fromkeys(candidates))


def _appears(value: str, added: str) -> bool:
    """추가된 줄에 값이 그대로 있는가. 헥사 색은 대소문자를 구분하지 않는다."""
    needle = value.strip()
    if not needle:
        return False
    flags = re.IGNORECASE if needle.startswith("#") else 0
    return re.search(re.escape(needle), added, flags) is not None
