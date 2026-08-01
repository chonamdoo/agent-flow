"""수치 원장 — 설계 phase가 남긴 값을 runner가 전 phase에 강제 주입한다.

phase 간 유일한 운반체가 "압축되는 host 대화 컨텍스트"였다. `render_envelope`가
이전 artifact를 하나도 넣지 않아서, 매 phase마다 수치가 기억에서 다시 꺼내졌다.
그래서 같은 입력에서도 결과가 매번 달라졌다.

관측자는 **runner 렌더러**다. 값을 쓰는 것은 agent지만, 그 값이 다음 phase의
프롬프트에 실리는지는 agent가 통제하지 못한다. 주입 여부만 보장한다.

**이 층이 증명하지 않는 것**: 준수. 16dp를 보고도 12dp를 쓰면 여기서는 아무도
안 잡는다. 원장의 정확성은 사람 확인(V3)이, 구현 대조는 수치 gate(V6)가 본다.
빈 원장도 주입은 성공한다 — 그때는 "기록된 값이 없다"가 그대로 실린다.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from agent_flow.core.command_evidence import is_concrete_test_selector
from agent_flow.core.markers import (
    completion_gate_marker_values,
    unfenced_markdown_text,
)

LEDGER_FILE = "design-spec.md"
LEDGER_SECTION = "design values"
LEDGER_MARKER = "design-values:"
SPEC_LEDGER_SECTION = "spec items"
SPEC_MARKER = "spec-items:"
MANUAL_SPEC_APPROVALS_FILE = "spec-manual-approvals.json"
SPEC_CAPTURE_FILE = "spec-capture.json"
SPEC_CONFIRMATION_FILE = "spec-confirmed.json"

# 원장을 만드는 phase. 여기서만 값이 들어오고, 나머지 phase는 읽기만 한다.
LEDGER_SOURCE_PHASES = frozenset({"design", "prd"})

_NONE_VALUES = frozenset({"none", "n/a", "na", "-"})


@dataclass(frozen=True)
class SpecItem:
    spec_id: str
    requirement: str
    verification: str

@dataclass(frozen=True)
class SpecParseResult:
    items: tuple[SpecItem, ...]
    errors: tuple[str, ...]

@dataclass(frozen=True)
class SpecChange:
    kind: str
    before: SpecItem | None
    after: SpecItem | None

    @property
    def spec_id(self) -> str:
        item = self.after or self.before
        assert item is not None
        return item.spec_id


@dataclass(frozen=True)
class RunTaskRecord:
    found: bool
    text: str
    digest: str
    sealed: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class DesignLedger:
    exists: bool
    source_phase: str
    values: tuple[tuple[str, str], ...]
    spec_items: tuple[SpecItem, ...] = ()
    source_digest: str = ""
    spec_digest: str = ""
    task_digest: str = ""
    errors: tuple[str, ...] = ()


def capture_design_ledger(run_dir: Path, phase_id: str, artifact_text: str) -> DesignLedger | None:
    """설계 artifact에서 값을 뽑아 원장 파일로 굳힌다. 원장 phase가 아니면 None."""
    if phase_id not in LEDGER_SOURCE_PHASES:
        return None
    values = parse_design_values(artifact_text)
    parsed = parse_spec_item_section(artifact_text)
    if parsed.errors:
        raise ValueError("; ".join(parsed.errors))
    active_items = _spec_baseline_items(run_dir, parsed.items, persist=True)
    task_record = read_run_task_record(run_dir)
    if task_record.errors:
        raise ValueError("; ".join(task_record.errors))
    source_digest = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    spec_digest = spec_set_digest(active_items)
    ledger = DesignLedger(
        exists=True,
        source_phase=phase_id,
        values=values,
        spec_items=active_items,
        source_digest=source_digest,
        spec_digest=spec_digest,
        task_digest=task_record.digest,
    )
    write_ledger(run_dir, ledger)
    _write_capture_state(run_dir, ledger, source_items=parsed.items)
    return ledger


def parse_design_values(text: str) -> tuple[tuple[str, str], ...]:
    """`## Design Values` 아래의 `key: value` 줄만 모은다.

    Completion Gate 파서와 같은 규율이다 — 들여쓴 줄과 코드 펜스는 건너뛰고,
    다음 `#` 헤딩에서 끊는다. 그러지 않으면 본문의 아무 콜론 줄이나 값으로
    둔갑한다.
    """
    values: list[tuple[str, str]] = []
    in_section = False
    for line in unfenced_markdown_text(text).splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("#"):
            heading = lowered.lstrip("#").strip()
            if heading == LEDGER_SECTION:
                in_section = True
                continue
            if in_section:
                break
            continue
        if not in_section:
            continue
        candidate = _strip_bullet(stripped)
        key, separator, value = candidate.partition(":")
        key = key.strip()
        value = value.strip()
        if separator != ":" or not key or not value:
            continue
        if value.lower() in _NONE_VALUES:
            continue
        values.append((key, value))
    return tuple(values)

def parse_spec_items(text: str) -> tuple[SpecItem, ...]:
    return parse_spec_item_section(text).items


def parse_spec_item_section(text: str) -> SpecParseResult:
    items: list[SpecItem] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    current_id = ""
    current_requirement = ""
    current_verification = ""
    current_verification_count = 0
    current_duplicate = False
    in_section = False
    section_seen = False
    item_pattern = re.compile(r"^SPEC-([1-9]\d*):\s*(.+)$", re.IGNORECASE)

    def finish_current() -> None:
        nonlocal current_id, current_requirement, current_verification
        nonlocal current_verification_count, current_duplicate
        if not current_id:
            return
        if current_verification_count == 0:
            errors.append(
                f"{current_id}: verify: test:<name> | symbol:<symbol>=<value> | manual "
                "(missing verifier)"
            )
        elif current_verification_count != 1:
            errors.append(
                f"{current_id}: exactly one verify line required "
                f"(found {current_verification_count})"
            )
        elif not _valid_spec_verification(current_verification):
            errors.append(
                f"{current_id}: verify: test:<name> | symbol:<symbol>=<value> | manual "
                f"(invalid verifier: {current_verification})"
            )
        elif not current_duplicate:
            items.append(
                SpecItem(
                    spec_id=current_id,
                    requirement=current_requirement,
                    verification=current_verification,
                )
            )
        current_id = ""
        current_requirement = ""
        current_verification = ""
        current_verification_count = 0
        current_duplicate = False

    for line in unfenced_markdown_text(text).splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("#"):
            heading = lowered.lstrip("#").strip()
            if heading == SPEC_LEDGER_SECTION:
                if section_seen:
                    errors.append("multiple ## Spec Items sections")
                    in_section = False
                else:
                    section_seen = True
                    in_section = True
                continue
            if in_section:
                finish_current()
                in_section = False
            continue
        if not in_section:
            continue
        candidate = _strip_bullet(stripped)
        match = item_pattern.fullmatch(candidate)
        if match:
            finish_current()
            current_id = f"SPEC-{int(match.group(1))}"
            current_requirement = match.group(2).strip()
            current_duplicate = current_id in seen_ids
            if current_duplicate:
                errors.append(f"{current_id}: duplicate SPEC id")
            else:
                seen_ids.add(current_id)
            continue
        if candidate.lower().startswith("spec-"):
            finish_current()
            errors.append(f"SPEC item has invalid syntax: {candidate}")
            continue
        if candidate.lower().startswith("verify:"):
            if not current_id:
                errors.append(f"{candidate} (verifier without a SPEC item)")
                continue
            current_verification_count += 1
            if current_verification_count == 1:
                current_verification = candidate.partition(":")[2].strip()
    finish_current()
    return SpecParseResult(items=tuple(items), errors=tuple(errors))


def _valid_spec_verification(value: str) -> bool:
    lowered = value.lower()
    if lowered == "manual":
        return True
    if "|" in value:
        return False
    if lowered.startswith("test:"):
        return is_concrete_test_selector(value.partition(":")[2])
    if not lowered.startswith("symbol:"):
        return False
    symbol, separator, expected = value.partition(":")[2].partition("=")
    return separator == "=" and bool(symbol.strip()) and bool(expected.strip())


def write_ledger(run_dir: Path, ledger: DesignLedger) -> Path:
    path = run_dir / LEDGER_FILE
    lines = [
        "# Design specification ledger",
        "",
        f"Source phase: {ledger.source_phase}",
        f"Source artifact SHA-256: {ledger.source_digest}",
        f"Spec set SHA-256: {ledger.spec_digest}",
    ]
    if ledger.task_digest:
        lines.append(f"Task SHA-256: {ledger.task_digest}")
    lines.extend(("", f"## {SPEC_LEDGER_SECTION.title()}", ""))
    for item in ledger.spec_items:
        lines.extend(
            (
                f"{item.spec_id}: {item.requirement}",
                f"verify: {item.verification}",
            )
        )
    if not ledger.spec_items:
        lines.append("(none recorded)")
    lines.extend(("", f"## {LEDGER_SECTION.title()}", ""))
    lines.extend(f"{key}: {value}" for key, value in ledger.values)
    if not ledger.values:
        lines.append("(none recorded)")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f"{path.suffix}.tmp")
    pending.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pending.replace(path)
    return path


def read_ledger(run_dir: Path) -> DesignLedger:
    path = run_dir / LEDGER_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return DesignLedger(exists=False, source_phase="", values=(), spec_items=())
    source = _ledger_header(text, "Source phase")
    source_digest = _ledger_header(text, "Source artifact SHA-256")
    recorded_spec_digest = _ledger_header(text, "Spec set SHA-256")
    recorded_task_digest = _ledger_header(text, "Task SHA-256")
    parsed = parse_spec_item_section(text)
    values = parse_design_values(text)
    calculated_spec_digest = spec_set_digest(parsed.items)
    task_record = read_run_task_record(run_dir)
    errors = [*parsed.errors, *task_record.errors]
    if source not in LEDGER_SOURCE_PHASES:
        errors.append(f"invalid source phase: {source or '<missing>'}")
    if not source_digest:
        errors.append("missing source artifact digest")
    if recorded_spec_digest != calculated_spec_digest:
        errors.append("SPEC set digest does not match design-spec.md")
    if parsed.items != _spec_baseline_items(run_dir, parsed.items):
        errors.append("SPEC baseline snapshot does not match design-spec.md")
    if task_record.sealed and not recorded_task_digest:
        errors.append("task digest is missing from design-spec.md")
    if recorded_task_digest and task_record.digest != recorded_task_digest:
        errors.append("run task does not match design-spec.md")
    capture = _read_json(run_dir / SPEC_CAPTURE_FILE)
    if not isinstance(capture, dict):
        # spec capture는 design-spec.md보다 늦게 도입됐다. 그 사이에 시작된 run은
        # 원장만 있고 capture가 없어 여기서 걸린다. 무엇이 없는지만 말하면 사용자는
        # 복구 경로를 못 찾는다.
        errors.append(
            "spec capture state is missing (this run's design-spec.md predates spec "
            "capture; redo the design/prd phase so the runner recaptures it)"
        )
    else:
        if capture.get("source_phase") != source:
            errors.append("source phase does not match spec capture state")
        if capture.get("source_digest") != source_digest:
            errors.append("source artifact digest does not match spec capture state")
        if capture.get("active_spec_digest") != calculated_spec_digest:
            errors.append("active SPEC set does not match spec capture state")
        if capture.get("active_spec_fingerprints") != [
            _spec_fingerprint(item) for item in parsed.items
        ]:
            errors.append("active SPEC fingerprints do not match spec capture state")
        if capture.get("task_digest", "") != recorded_task_digest:
            errors.append("task does not match spec capture state")
        if capture.get("active_design_values_digest") != _design_values_digest(values):
            errors.append("design values do not match spec capture state")
    source_path = _source_artifact_path(run_dir, source)
    if source_path is None:
        errors.append("source artifact is missing")
    else:
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except OSError:
            errors.append("source artifact cannot be read")
        else:
            source_specs = parse_spec_item_section(source_text)
            if source_specs.errors:
                errors.append("source artifact SPEC items are invalid")
            source_values = parse_design_values(source_text)
            if (
                source_values != values
                and (
                    not isinstance(capture, dict)
                    or capture.get("unconfirmed_design_values_digest")
                    != _design_values_digest(source_values)
                )
            ):
                errors.append("source artifact design values do not match design-spec.md")
    return DesignLedger(
        exists=True,
        source_phase=source,
        values=values,
        spec_items=parsed.items,
        source_digest=source_digest,
        spec_digest=recorded_spec_digest,
        task_digest=recorded_task_digest,
        errors=tuple(dict.fromkeys(errors)),
    )

def read_manual_spec_approvals(run_dir: Path) -> frozenset[str]:
    payload = _read_json(run_dir / MANUAL_SPEC_APPROVALS_FILE)
    if not isinstance(payload, dict) or not isinstance(payload.get("approvals"), list):
        return frozenset()
    ledger = read_ledger(run_dir)
    if ledger.errors:
        return frozenset()
    expected = {
        item.spec_id: item
        for item in ledger.spec_items
        if item.verification.strip().lower() == "manual"
    }
    approved: set[str] = set()
    for raw in payload["approvals"]:
        if not isinstance(raw, dict):
            continue
        spec_id = str(raw.get("spec_id", "")).upper()
        item = expected.get(spec_id)
        if item is None:
            continue
        fingerprint = _spec_fingerprint(item)
        if (
            raw.get("spec_fingerprint") == fingerprint
            and raw.get("statement") == _manual_approval_statement(item)
        ):
            approved.add(spec_id)
    return frozenset(approved)


def manual_spec_approval_statement(run_dir: Path, spec_id: str) -> str:
    item = _manual_spec_item(run_dir, spec_id)
    return _manual_approval_statement(item)


def record_manual_spec_approval(
    run_dir: Path,
    spec_id: str,
    statement: str,
) -> Path:
    item = _manual_spec_item(run_dir, spec_id)
    expected_statement = _manual_approval_statement(item)
    if statement.strip() != expected_statement:
        raise ValueError(f"approval statement must be: {expected_statement}")
    approved = read_manual_spec_approvals(run_dir) | {item.spec_id}
    ledger = read_ledger(run_dir)
    manual_items = {
        candidate.spec_id: candidate
        for candidate in ledger.spec_items
        if candidate.verification.strip().lower() == "manual"
    }
    approvals = [
        {
            "spec_id": approved_id,
            "spec_fingerprint": _spec_fingerprint(manual_items[approved_id]),
            "statement": _manual_approval_statement(manual_items[approved_id]),
        }
        for approved_id in sorted(approved)
        if approved_id in manual_items
    ]
    path = run_dir / MANUAL_SPEC_APPROVALS_FILE
    _write_json_atomic(path, {"approvals": approvals})
    return path


def spec_set_digest(items: tuple[SpecItem, ...]) -> str:
    payload = "\0".join(_spec_fingerprint(item) for item in items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _spec_baseline_items(
    run_dir: Path,
    candidate_items: tuple[SpecItem, ...],
    *,
    persist: bool = False,
) -> tuple[SpecItem, ...]:
    confirmation_path = run_dir / SPEC_CONFIRMATION_FILE
    if confirmation_path.is_file():
        return read_confirmed_spec_items(run_dir)
    baseline = candidate_items
    try:
        ledger_text = (run_dir / LEDGER_FILE).read_text(encoding="utf-8")
    except OSError:
        ledger_text = ""
    if ledger_text:
        legacy = parse_spec_item_section(ledger_text)
        if not legacy.errors and legacy.items:
            baseline = legacy.items
    if persist:
        record_spec_confirmation(run_dir, baseline)
    return baseline


def read_confirmed_spec_items(run_dir: Path) -> tuple[SpecItem, ...]:
    payload = _read_json(run_dir / SPEC_CONFIRMATION_FILE)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return ()
    items: list[SpecItem] = []
    seen: set[str] = set()
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            return ()
        spec_id = raw.get("spec_id")
        requirement = raw.get("requirement")
        verification = raw.get("verification")
        if (
            not isinstance(spec_id, str)
            or re.fullmatch(r"SPEC-[1-9]\d*", spec_id) is None
            or spec_id in seen
            or not isinstance(requirement, str)
            or not requirement.strip()
            or not isinstance(verification, str)
            or not _valid_spec_verification(verification)
        ):
            return ()
        seen.add(spec_id)
        items.append(
            SpecItem(
                spec_id=spec_id,
                requirement=requirement.strip(),
                verification=verification.strip(),
            )
        )
    return tuple(items)


def record_spec_confirmation(
    run_dir: Path,
    items: tuple[SpecItem, ...],
) -> Path:
    path = run_dir / SPEC_CONFIRMATION_FILE
    _write_json_atomic(
        path,
        {
            "items": [
                {
                    "spec_id": item.spec_id,
                    "requirement": item.requirement,
                    "verification": item.verification,
                }
                for item in items
            ]
        },
    )
    return path


def pending_spec_changes(
    run_dir: Path,
    candidate_items: tuple[SpecItem, ...],
) -> tuple[SpecChange, ...]:
    confirmed = _spec_baseline_items(run_dir, candidate_items)
    confirmed_by_id = {item.spec_id: item for item in confirmed}
    candidate_by_id = {item.spec_id: item for item in candidate_items}
    changes: list[SpecChange] = []
    for item in candidate_items:
        previous = confirmed_by_id.get(item.spec_id)
        if previous is None:
            changes.append(SpecChange("added", None, item))
        elif previous != item:
            changes.append(SpecChange("modified", previous, item))
    for item in confirmed:
        if item.spec_id not in candidate_by_id:
            changes.append(SpecChange("deleted", item, None))
    return tuple(changes)


def current_spec_source(
    run_dir: Path,
) -> tuple[str, Path, SpecParseResult]:
    phase_id = ""
    try:
        ledger_text = (run_dir / LEDGER_FILE).read_text(encoding="utf-8")
    except OSError:
        ledger_text = ""
    if ledger_text:
        phase_id = _ledger_header(ledger_text, "Source phase")
    candidates: list[tuple[str, Path | None]] = []
    if phase_id:
        candidates.append((phase_id, _source_artifact_path(run_dir, phase_id)))
    candidates.extend(
        (candidate_phase, _source_artifact_path(run_dir, candidate_phase))
        for candidate_phase in ("design", "prd")
        if candidate_phase != phase_id
    )
    for candidate_phase, path in candidates:
        if path is None:
            continue
        parsed = parse_spec_item_section(path.read_text(encoding="utf-8"))
        if parsed.errors:
            raise ValueError("; ".join(parsed.errors))
        return candidate_phase, path, parsed
    raise ValueError(f"run has no SPEC source artifact: {run_dir}")


def pending_spec_changes_for_run(run_dir: Path) -> tuple[SpecChange, ...]:
    try:
        _, _, parsed = current_spec_source(run_dir)
    except ValueError:
        return ()
    return pending_spec_changes(run_dir, parsed.items)


def confirm_current_spec_changes(run_dir: Path) -> Path:
    phase_id, source_path, parsed = current_spec_source(run_dir)
    if not parsed.items:
        raise ValueError("SPEC source artifact has no items")
    source_text = source_path.read_text(encoding="utf-8")
    ledger = read_ledger(run_dir)
    if not ledger.exists:
        confirmation_path = record_spec_confirmation(run_dir, parsed.items)
        capture_design_ledger(run_dir, phase_id, source_text)
        return confirmation_path
    if not pending_spec_changes(run_dir, parsed.items):
        raise ValueError("no pending SPEC changes to confirm")
    expected_source_error = "source artifact design values do not match design-spec.md"
    capture = _read_json(run_dir / SPEC_CAPTURE_FILE)
    can_snapshot_unconfirmed_values = (
        isinstance(capture, dict)
        and not capture.get("unconfirmed_design_values_digest")
    )
    blocking_errors = tuple(
        error
        for error in ledger.errors
        if error != expected_source_error or not can_snapshot_unconfirmed_values
    )
    if blocking_errors:
        detail = "; ".join(blocking_errors)
        raise ValueError(f"cannot confirm SPEC changes: {detail}")
    source_values = parse_design_values(source_text)
    confirmation_path = record_spec_confirmation(run_dir, parsed.items)
    updated = DesignLedger(
        exists=True,
        source_phase=ledger.source_phase,
        values=ledger.values,
        spec_items=parsed.items,
        source_digest=ledger.source_digest,
        spec_digest=spec_set_digest(parsed.items),
        task_digest=ledger.task_digest,
    )
    write_ledger(run_dir, updated)
    _write_capture_state(
        run_dir,
        updated,
        source_items=parsed.items,
        unconfirmed_design_values=(
            source_values if source_values != ledger.values else None
        ),
    )
    return confirmation_path


def render_spec_changes(changes: tuple[SpecChange, ...]) -> str:
    if not changes:
        return "No SPEC changes require confirmation."
    sections: list[str] = []
    for kind, title in (
        ("added", "Added"),
        ("modified", "Modified"),
        ("deleted", "Deleted"),
    ):
        selected = [change for change in changes if change.kind == kind]
        if not selected:
            continue
        lines = [f"### {title}"]
        for change in selected:
            if kind == "added":
                assert change.after is not None
                lines.extend(
                    (
                        f"- {change.after.spec_id}: {change.after.requirement}",
                        f"  verify: {change.after.verification}",
                    )
                )
            elif kind == "deleted":
                assert change.before is not None
                lines.extend(
                    (
                        f"- {change.before.spec_id}: {change.before.requirement}",
                        f"  verify: {change.before.verification}",
                    )
                )
            else:
                assert change.before is not None and change.after is not None
                lines.append(f"- {change.spec_id}")
                if change.before.requirement != change.after.requirement:
                    lines.extend(
                        (
                            f"  requirement before: {change.before.requirement}",
                            f"  requirement after: {change.after.requirement}",
                        )
                    )
                if change.before.verification != change.after.verification:
                    lines.extend(
                        (
                            f"  verify before: {change.before.verification}",
                            f"  verify after: {change.after.verification}",
                        )
                    )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _manual_spec_item(run_dir: Path, spec_id: str) -> SpecItem:
    canonical_id = spec_id.strip().upper()
    if not re.fullmatch(r"SPEC-[1-9]\d*", canonical_id):
        raise ValueError(f"invalid SPEC id: {spec_id}")
    ledger = read_ledger(run_dir)
    if ledger.errors:
        raise ValueError(f"design-spec.md is invalid: {'; '.join(ledger.errors)}")
    item = next(
        (
            candidate
            for candidate in ledger.spec_items
            if candidate.spec_id == canonical_id
        ),
        None,
    )
    if item is None:
        raise ValueError(f"unknown SPEC id: {canonical_id}")
    if item.verification.strip().lower() != "manual":
        raise ValueError(f"{canonical_id} does not use manual verification")
    return item

def _manual_approval_statement(item: SpecItem) -> str:
    return f"APPROVE {item.spec_id} {_spec_fingerprint(item)[:12]}"


def _spec_fingerprint(item: SpecItem) -> str:
    payload = "\0".join((item.spec_id, item.requirement, item.verification))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _design_values_digest(values: tuple[tuple[str, str], ...]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_capture_state(
    run_dir: Path,
    ledger: DesignLedger,
    *,
    source_items: tuple[SpecItem, ...],
    unconfirmed_design_values: tuple[tuple[str, str], ...] | None = None,
) -> None:
    _write_json_atomic(
        run_dir / SPEC_CAPTURE_FILE,
        {
            "source_phase": ledger.source_phase,
            "source_digest": ledger.source_digest,
            "source_spec_digest": spec_set_digest(source_items),
            "source_spec_fingerprints": [
                _spec_fingerprint(item) for item in source_items
            ],
            "active_spec_digest": ledger.spec_digest,
            "active_spec_fingerprints": [
                _spec_fingerprint(item) for item in ledger.spec_items
            ],
            "active_design_values_digest": _design_values_digest(ledger.values),
            "task_digest": ledger.task_digest,
            "unconfirmed_design_values_digest": (
                _design_values_digest(unconfirmed_design_values)
                if unconfirmed_design_values is not None
                else ""
            ),
        },
    )


def _ledger_header(text: str, name: str) -> str:
    prefix = f"{name}:".lower()
    for line in text.splitlines():
        if line.lower().startswith(prefix):
            return line.partition(":")[2].strip()
    return ""


def _source_artifact_path(run_dir: Path, phase_id: str) -> Path | None:
    for relative in (f"{phase_id}.md", f"artifacts/{phase_id}.md"):
        candidate = run_dir / relative
        if candidate.is_file():
            return candidate
    return None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f"{path.suffix}.tmp")
    pending.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pending.replace(path)


def _source_artifact_exists(run_dir: Path) -> bool:
    return any(
        (run_dir / relative).is_file()
        for phase_id in LEDGER_SOURCE_PHASES
        for relative in (f"{phase_id}.md", f"artifacts/{phase_id}.md")
    )


def read_run_task_record(run_dir: Path) -> RunTaskRecord:
    records: list[tuple[str, str, str | None]] = []
    errors: list[str] = []
    for name in ("meta.json", "manifest.json"):
        path = run_dir / name
        if not path.exists():
            continue
        payload = _read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), str):
            errors.append(f"{name} has no valid task")
            continue
        text = payload["task"].strip()
        declared = payload.get("task_digest")
        if declared is not None and not isinstance(declared, str):
            errors.append(f"{name} has an invalid task digest")
            declared = None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if declared is not None and declared != digest:
            errors.append(f"{name} task digest does not match task")
        records.append((name, text, declared))
    if not records:
        return RunTaskRecord(False, "", "", False, tuple(errors))
    texts = {text for _, text, _ in records}
    if len(texts) != 1:
        errors.append("run task differs between metadata files")
    declarations = {declared for _, _, declared in records}
    if None in declarations and len(declarations) > 1:
        errors.append("task digest is missing from one metadata file")
    nonempty_declarations = {value for value in declarations if value is not None}
    if len(nonempty_declarations) > 1:
        errors.append("task digest differs between metadata files")
    text = records[0][1]
    return RunTaskRecord(
        True,
        text,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        all(declared is not None for _, _, declared in records),
        tuple(dict.fromkeys(errors)),
    )


def task_text_integrity_errors(run_dir: Path, task_text: str) -> tuple[str, ...]:
    record = read_run_task_record(run_dir)
    errors = list(record.errors)
    if record.found and record.text != task_text.strip():
        errors.append("completion task does not match run metadata")
    return tuple(dict.fromkeys(errors))


def _run_task_text(run_dir: Path) -> str:
    return read_run_task_record(run_dir).text


def ledger_prompt_block(run_dir: Path) -> str:
    """전 phase 엔벨로프에 실리는 블록. agent가 끌 수 없다."""
    ledger = read_ledger(run_dir)
    if not ledger.exists:
        if _source_artifact_exists(run_dir):
            raise RuntimeError("design-spec.md is missing after the source phase")
        return ""
    if ledger.errors:
        raise RuntimeError(
            f"design-spec.md is invalid: {'; '.join(ledger.errors)}. "
            "This block is injected into every phase prompt, so the run cannot "
            "advance until it is valid: redo the source phase, or start a new run."
        )
    pending_changes = pending_spec_changes_for_run(run_dir)
    if _run_task_text(run_dir) and not ledger.spec_items and not pending_changes:
        raise RuntimeError("design-spec.md has no SPEC items for a non-empty task")
    header = (
        "\n## Design specification ledger (injected by the runner)\n\n"
        f"Recorded in the `{ledger.source_phase}` phase and carried into every "
        "phase of this run. You did not opt in to this block and cannot suppress "
        "it; it is the only thing that survives a compacted conversation.\n\n"
    )
    if ledger.spec_items:
        specs = "\n".join(
            f"{item.spec_id}: {item.requirement}\nverify: {item.verification}"
            for item in ledger.spec_items
        )
        manual_instruction = (
            "For a manual verification, ask the user in this chat. After a clear "
            "affirmative reply, run `agent-flow spec approve <SPEC-ID> --run-dir "
            "<run-dir>` yourself. Do not require an exact phrase and never ask the "
            "user to enter a terminal command.\n\n"
            if any(item.verification.strip().lower() == "manual" for item in ledger.spec_items)
            else ""
        )
        spec_block = (
            "### Confirmed Spec Items\n\n"
            f"{specs}\n\n"
            "Every confirmed SPEC item must be satisfied by its recorded "
            "verification evidence.\n\n"
            f"{manual_instruction}"
        )
    else:
        spec_block = "No SPEC items have been confirmed yet.\n\n"
    if pending_changes:
        changes = render_spec_changes(pending_changes)
        pending_block = (
            "### SPEC Changes Awaiting User Confirmation\n\n"
            f"{changes}\n\n"
            "Show only these added, modified, or deleted items to the user and ask "
            "whether to apply them. Any clear affirmative reply is enough; do not "
            "require an exact token and do not ask the user to run a terminal "
            "command. After the user confirms, run `agent-flow spec confirm "
            f"--run-dir {shlex.quote(str(run_dir))}` yourself. Until then, do not start added or "
            "modified SPEC work. Unchanged confirmed SPEC work may continue, and "
            "the previously confirmed version of modified or deleted items remains "
            "active.\n\n"
        )
    else:
        pending_block = ""
    if ledger.values:
        values = "\n".join(f"- `{key}`: `{value}`" for key, value in ledger.values)
        value_block = (
            "### Design Values\n\n"
            f"{values}\n\n"
            "Use these exact values. If one of them is wrong, say so and stop — "
            "do not silently substitute a different number.\n"
        )
    else:
        value_block = (
            "No design values were recorded for this run. Do not invent numbers, "
            "colors, or tokens that are not in the task or the codebase.\n"
        )
    return header + spec_block + pending_block + value_block


def _strip_bullet(line: str) -> str:
    candidate = line.strip()
    if candidate.startswith("+"):
        candidate = candidate[1:].strip()
    for prefix in ("- [x] ", "- [ ] ", "- ", "* ", "| "):
        if candidate.lower().startswith(prefix):
            return candidate[len(prefix):].strip().strip("|").strip()
    return candidate


def missing_design_value_markers(text: str, phase_id: str) -> list[str]:
    """원장 마커가 본문과 어긋나면 막는다.

    마커만 있으면 `design-values: none` 한 줄로 수치 기록 전체를 건너뛸 수
    있고, `design-values-confirmed: n/a`는 사람 확인을 건너뛰는 기본값이 된다.
    본문과 대조해야 escape hatch가 기본값이 되지 않는다.
    """
    if phase_id not in LEDGER_SOURCE_PHASES:
        return []
    values = parse_design_values(text)
    recorded = completion_gate_marker_values(text)
    missing: list[str] = []
    declared = recorded.get("design-values", "").strip().lower()
    if values and declared in _NONE_VALUES:
        keys = ", ".join(key for key, _ in values)
        missing.append(f"design-values: {keys} (the section records values; 'none' contradicts it)")
    if not values and declared and declared not in _NONE_VALUES:
        missing.append("design-values: none (the ## Design Values section is empty)")
    confirmed = recorded.get("design-values-confirmed", "").strip().lower()
    if confirmed and values and confirmed not in {"yes", "y"}:
        missing.append(
            "design-values-confirmed: yes (values were recorded, so the user has to "
            "confirm your reading; 'n/a' is only for a task with no supplied values)"
        )
    return missing
