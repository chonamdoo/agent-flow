"""수치 원장 테스트.

원장이 지키는 것은 **전달**뿐이다. 준수는 지키지 않는다 — 16dp를 보고 12dp를
써도 여기서는 안 잡힌다. 그래서 테스트도 전달만 반증한다: 원장이 있으면 모든
phase 엔벨로프에 실리고, agent가 그것을 끌 수 없다.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor

import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.adapters.generic import GenericAdapter
from agent_flow.core import design_ledger as DESIGN_LEDGER, design_value_check
from agent_flow.core.design_ledger import (
    LEDGER_FILE,
    MANUAL_SPEC_APPROVALS_FILE,
    SPEC_CAPTURE_FILE,
    SPEC_CONFIRMATION_FILE,
    capture_design_ledger,
    confirm_current_spec_changes,
    manual_spec_approval_statement,
    ledger_prompt_block,
    missing_design_value_markers,
    parse_declared_concerns,
    parse_design_values,
    parse_spec_item_section,
    pending_spec_changes_for_run,
    read_ledger,
    read_manual_spec_approvals,
    record_manual_spec_approval,
    render_spec_changes,
)
from agent_flow.runner import Phase



ARTIFACT = """# prd

## Design Values

horizontal-padding: 16dp
brand-primary: #FF6B00
list-row-height: 56dp

## Completion Gate

design-values: horizontal-padding, brand-primary, list-row-height
design-values-confirmed: yes
"""

SPEC_ARTIFACT = """# design

## Spec Items

SPEC-1: Empty search results show the empty state.
verify: test:test_empty_search_results_show_the_empty_state
SPEC-2: SearchResults uses the empty-state copy.
verify: symbol:SearchResults=No results

## Design Values

## Completion Gate

spec-items: SPEC-1, SPEC-2
design-values: none
"""


def _capture(run_dir: Path, phase_id: str, artifact: str):
    (run_dir / f"{phase_id}.md").write_text(artifact, encoding="utf-8")
    return capture_design_ledger(run_dir, phase_id, artifact)


def test_parses_only_the_design_values_section():
    """반증: 본문 아무 콜론 줄이나 값이 되면 원장이 쓰레기로 찬다."""
    text = "# prd\n\nowner: someone\n\n## Design Values\n\ngap: 8dp\n\n## Risks\n\nlatency: high\n"
    assert parse_design_values(text) == (("gap", "8dp"),)


def test_fenced_and_indented_lines_are_not_values():
    text = "## Design Values\n\n```\nfake: 1dp\n```\n\n    also-fake: 2dp\nreal: 3dp\n"
    assert parse_design_values(text) == (("real", "3dp"),)


def test_inline_code_span_does_not_open_a_fence():
    """반증: 한 줄짜리 코드 스팬이 fence로 오인되면 뒤 본문이 통째로 사라진다."""
    text = "## Design Values\n\n```dp``` is the unit\ngap: 8dp\n"
    assert parse_design_values(text) == (("gap", "8dp"),)


def test_shorter_fence_run_does_not_close_a_longer_one():
    text = "## Design Values\n\n````\nfake: 1dp\n```\nalso-fake: 2dp\n````\nreal: 3dp\n"
    assert parse_design_values(text) == (("real", "3dp"),)


def test_spec_items_inside_a_fence_are_not_recorded():
    text = (
        "## Spec Items\n\n"
        "~~~markdown\n"
        "SPEC-9: fenced example\n"
        "verify: manual\n"
        "~~~\n"
        "SPEC-1: real requirement\n"
        "verify: manual\n"
    )
    parsed = parse_spec_item_section(text)
    assert parsed.errors == ()
    assert tuple(item.spec_id for item in parsed.items) == ("SPEC-1",)



def test_bullet_values_are_accepted():
    text = "## Design Values\n\n- gap: 8dp\n* radius: 4dp\n"
    assert parse_design_values(text) == (("gap", "8dp"), ("radius", "4dp"))


def test_none_placeholder_is_not_a_value():
    assert parse_design_values("## Design Values\n\ngap: none\n") == ()


def test_capture_writes_the_ledger_file(tmp_path):
    ledger = _capture(tmp_path, "prd", ARTIFACT)
    assert ledger is not None
    assert (tmp_path / LEDGER_FILE).is_file()
    assert read_ledger(tmp_path).values == (
        ("horizontal-padding", "16dp"),
        ("brand-primary", "#FF6B00"),
        ("list-row-height", "56dp"),
    )


def test_spec_items_round_trip_and_prompt_injection(tmp_path):
    _capture(tmp_path, "design", SPEC_ARTIFACT)

    ledger_text = (tmp_path / LEDGER_FILE).read_text(encoding="utf-8")
    prompt = ledger_prompt_block(tmp_path)

    assert "SPEC-1: Empty search results show the empty state." in ledger_text
    assert "verify: test:test_empty_search_results_show_the_empty_state" in ledger_text
    assert "SPEC-2: SearchResults uses the empty-state copy." in prompt
    assert "verify: symbol:SearchResults=No results" in prompt

def test_initial_spec_set_is_baselined_without_separate_confirmation(tmp_path):
    ledger = _capture(tmp_path, "design", SPEC_ARTIFACT)

    assert tuple(item.spec_id for item in ledger.spec_items) == ("SPEC-1", "SPEC-2")
    assert pending_spec_changes_for_run(tmp_path) == ()
    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        SPEC_ARTIFACT,
        task_text="Implement both requirements.",
    ) == []
    prompt = ledger_prompt_block(tmp_path)
    assert "SPEC Changes Awaiting User Confirmation" not in prompt
    assert "agent-flow spec confirm" not in prompt


def test_later_addition_is_reported_without_replacing_the_active_baseline(tmp_path):
    _capture(tmp_path, "design", SPEC_ARTIFACT)
    changed = SPEC_ARTIFACT.replace(
        "\n## Design Values",
        "\nSPEC-3: Empty state offers a retry action.\n"
        "verify: test:test_empty_state_retry\n\n"
        "## Design Values",
    )
    (tmp_path / "design.md").write_text(changed, encoding="utf-8")

    changes = pending_spec_changes_for_run(tmp_path)
    prompt = ledger_prompt_block(tmp_path)
    rendered = render_spec_changes(changes)

    assert [(change.kind, change.spec_id) for change in changes] == [
        ("added", "SPEC-3")
    ]
    assert "### Added" in rendered
    assert "SPEC-3: Empty state offers a retry action." in rendered
    assert "### Confirmed Spec Items" in prompt
    assert "SPEC-1: Empty search results show the empty state." in prompt
    assert "### SPEC Changes Awaiting User Confirmation" in prompt
    assert "SPEC-3: Empty state offers a retry action." in prompt
    assert "Unchanged confirmed SPEC work may continue" in prompt
    assert "do not start added or modified SPEC work" in prompt
    assert "agent-flow spec confirm" in prompt
    assert "do not ask the user to run a terminal command" in prompt


def test_pending_prompt_shell_quotes_the_run_path(tmp_path):
    run_dir = tmp_path / "run $'quoted"
    run_dir.mkdir()
    _capture(run_dir, "design", SPEC_ARTIFACT)
    changed = SPEC_ARTIFACT.replace(
        "\n## Design Values",
        "\nSPEC-3: Empty state offers a retry action.\n"
        "verify: test:test_empty_state_retry\n\n"
        "## Design Values",
    )
    (run_dir / "design.md").write_text(changed, encoding="utf-8")

    prompt = ledger_prompt_block(run_dir)

    assert (
        "agent-flow spec confirm --run-dir "
        f"{shlex.quote(str(run_dir))}"
    ) in prompt


def test_modified_and_deleted_items_require_only_delta_confirmation(tmp_path):
    _capture(tmp_path, "design", SPEC_ARTIFACT)
    changed = """# design

## Spec Items

SPEC-1: Empty search results show a retry action.
verify: test:test_empty_search_results_show_the_empty_state

## Completion Gate

spec-items: SPEC-1
design-values: none
"""
    (tmp_path / "design.md").write_text(changed, encoding="utf-8")

    changes = pending_spec_changes_for_run(tmp_path)
    assert [(change.kind, change.spec_id) for change in changes] == [
        ("modified", "SPEC-1"),
        ("deleted", "SPEC-2"),
    ]
    rendered = render_spec_changes(changes)
    assert "### Modified" in rendered
    assert "requirement before: Empty search results show the empty state." in rendered
    assert "requirement after: Empty search results show a retry action." in rendered
    assert "### Deleted" in rendered
    assert read_ledger(tmp_path).spec_items == parse_spec_item_section(
        SPEC_ARTIFACT
    ).items

    confirm_current_spec_changes(tmp_path)

    assert pending_spec_changes_for_run(tmp_path) == ()
    active = read_ledger(tmp_path).spec_items
    assert tuple(item.spec_id for item in active) == ("SPEC-1",)
    assert active[0].requirement == "Empty search results show a retry action."


def test_spec_confirmation_preserves_unshown_design_values(tmp_path):
    initial = SPEC_ARTIFACT.replace(
        "\n## Completion Gate",
        "\nspacing: 16dp\n\n## Completion Gate",
    )
    before = _capture(tmp_path, "design", initial)
    changed = initial.replace(
        "Empty search results show the empty state.",
        "Empty search results show a retry action.",
    ).replace("spacing: 16dp", "spacing: 24dp")
    (tmp_path / "design.md").write_text(changed, encoding="utf-8")

    confirm_current_spec_changes(tmp_path)

    after = read_ledger(tmp_path)
    assert pending_spec_changes_for_run(tmp_path) == ()
    assert after.spec_items[0].requirement == "Empty search results show a retry action."
    assert after.values == before.values == (("spacing", "16dp"),)
    assert after.source_digest == before.source_digest

    (tmp_path / "design.md").write_text(
        changed.replace("spacing: 24dp", "spacing: 32dp"),
        encoding="utf-8",
    )
    assert (
        "source artifact design values do not match design-spec.md"
        in read_ledger(tmp_path).errors
    )

    changed_again = changed.replace(
        "\n## Design Values",
        "\nSPEC-3: Empty state offers a retry action.\n"
        "verify: test:test_empty_state_retry\n\n"
        "## Design Values",
    ).replace("spacing: 24dp", "spacing: 32dp")
    (tmp_path / "design.md").write_text(changed_again, encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="source artifact design values do not match",
    ):
        confirm_current_spec_changes(tmp_path)


def test_spec_confirmation_rejects_tampered_ledger_design_values(tmp_path):
    initial = SPEC_ARTIFACT.replace(
        "\n## Completion Gate",
        "\nspacing: 16dp\n\n## Completion Gate",
    )
    _capture(tmp_path, "design", initial)
    ledger_path = tmp_path / LEDGER_FILE
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").replace(
            "spacing: 16dp",
            "spacing: 24dp",
        ),
        encoding="utf-8",
    )
    (tmp_path / "design.md").write_text(
        initial.replace(
            "Empty search results show the empty state.",
            "Empty search results show a retry action.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="design values do not match spec capture state",
    ):
        confirm_current_spec_changes(tmp_path)


def test_design_value_only_change_cannot_use_spec_confirmation(tmp_path):
    initial = SPEC_ARTIFACT.replace(
        "\n## Completion Gate",
        "\nspacing: 16dp\n\n## Completion Gate",
    )
    _capture(tmp_path, "design", initial)
    (tmp_path / "design.md").write_text(
        initial.replace("spacing: 16dp", "spacing: 24dp"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no pending SPEC changes"):
        confirm_current_spec_changes(tmp_path)


def test_ledger_tampering_or_missing_capture_state_fails_closed(tmp_path):
    _capture(tmp_path, "design", SPEC_ARTIFACT)
    ledger_path = tmp_path / LEDGER_FILE
    original = ledger_path.read_text(encoding="utf-8")
    ledger_path.write_text(
        original.replace(
            "Empty search results show the empty state.",
            "Empty search results show a retry action.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="design-spec.md is invalid"):
        ledger_prompt_block(tmp_path)

    ledger_path.write_text(original, encoding="utf-8")
    (tmp_path / SPEC_CAPTURE_FILE).unlink()
    with pytest.raises(RuntimeError, match="spec capture state is missing"):
        ledger_prompt_block(tmp_path)


def test_manual_approval_rejects_forged_record_without_exact_statement(tmp_path):
    artifact = """## Spec Items

SPEC-1: Confirm the rendered copy.
verify: manual
"""
    _capture(tmp_path, "design", artifact)
    capture_state = json.loads(
        (tmp_path / SPEC_CAPTURE_FILE).read_text(encoding="utf-8")
    )
    fingerprint = capture_state["active_spec_fingerprints"][0]
    (tmp_path / MANUAL_SPEC_APPROVALS_FILE).write_text(
        json.dumps(
            {
                "approvals": [
                    {
                        "spec_id": "SPEC-1",
                        "spec_fingerprint": fingerprint,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert read_manual_spec_approvals(tmp_path) == frozenset()


def test_manual_spec_prompt_requires_explicit_user_approval(tmp_path):
    _capture(
        tmp_path,
        "design",
        """## Spec Items

SPEC-1: Confirm the rendered copy.
verify: manual
""",
    )

    prompt = ledger_prompt_block(tmp_path)

    assert "For a manual verification, ask the user in this chat." in prompt
    assert "agent-flow spec approve <SPEC-ID> --run-dir <run-dir>" in prompt


def test_concurrent_manual_spec_approvals_preserve_both(tmp_path):
    artifact = "## Spec Items\n\n" + "\n".join(
        f"SPEC-{index}: Confirm item {index}.\nverify: manual"
        for index in range(1, 17)
    )
    _capture(tmp_path, "design", artifact)
    approvals = [
        (
            f"SPEC-{index}",
            manual_spec_approval_statement(tmp_path, f"SPEC-{index}"),
        )
        for index in range(1, 17)
    ]

    def approve(item: tuple[str, str]) -> None:
        spec_id, statement = item
        record_manual_spec_approval(tmp_path, spec_id, statement)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(approve, approvals))

    assert read_manual_spec_approvals(tmp_path) == frozenset(
        spec_id for spec_id, _ in approvals
    )


@pytest.mark.parametrize("failing_writer", ["write_ledger", "_write_capture_state"])
def test_spec_confirmation_replays_after_each_intent_step(
    tmp_path, monkeypatch, failing_writer
):
    _capture(tmp_path, "design", SPEC_ARTIFACT)
    changed = SPEC_ARTIFACT.replace(
        "Empty search results show the empty state.",
        "Empty search results show a retry action.",
    )
    (tmp_path / "design.md").write_text(changed, encoding="utf-8")
    original = getattr(DESIGN_LEDGER, failing_writer)

    def interrupt(*args, **kwargs):
        raise OSError("injected interruption")

    monkeypatch.setattr(DESIGN_LEDGER, failing_writer, interrupt)
    with pytest.raises(OSError, match="injected interruption"):
        confirm_current_spec_changes(tmp_path)
    monkeypatch.setattr(DESIGN_LEDGER, failing_writer, original)

    confirmation = confirm_current_spec_changes(tmp_path)

    assert confirmation == tmp_path / SPEC_CONFIRMATION_FILE
    assert pending_spec_changes_for_run(tmp_path) == ()
    assert read_ledger(tmp_path).errors == ()

def test_source_spec_items_require_one_valid_verifier(tmp_path):
    artifact = """## Spec Items

SPEC-1: Empty search results show the empty state.

## Completion Gate

spec-items: SPEC-1
"""
    check = getattr(design_value_check, "missing_spec_item_evidence", None)

    assert check is not None
    assert check(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="Add an empty state for search results.",
    ) == [
        "SPEC-1: verify: test:<name> | symbol:<symbol>=<value> | manual "
        "(missing verifier)"
    ]

    duplicate_verifier = """## Spec Items

SPEC-1: Empty search results show the empty state.
verify: test:test_empty_search_results_show_the_empty_state
verify: manual

## Completion Gate

spec-items: SPEC-1
"""
    assert check(
        tmp_path,
        tmp_path,
        "design",
        duplicate_verifier,
        task_text="Add an empty state for search results.",
    ) == ["SPEC-1: exactly one verify line required (found 2)"]


def test_source_spec_items_reject_multiple_lists(tmp_path):
    artifact = (
        SPEC_ARTIFACT
        + "\n## Spec Items\n\n"
        + "SPEC-3: Hidden second list item.\n"
        + "verify: manual\n"
    )

    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="Apply all requirements.",
    ) == ["multiple ## Spec Items sections"]


def test_source_spec_items_reject_malformed_or_orphan_verifiers(tmp_path):
    artifact = """## Spec Items

SPEC-zero: Malformed requirement.
verify: manual
SPEC-1: Composite verifier.
verify: test:test_empty | manual

## Completion Gate

spec-items: SPEC-1
"""

    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="Apply both requirements.",
    ) == [
        "SPEC item has invalid syntax: SPEC-zero: Malformed requirement.",
        "verify: manual (verifier without a SPEC item)",
        "SPEC-1: verify: test:<name> | symbol:<symbol>=<value> | manual "
        "(invalid verifier: test:test_empty | manual)",
    ]


def test_source_spec_items_marker_matches_the_ordered_set(tmp_path):
    artifact = SPEC_ARTIFACT.replace(
        "spec-items: SPEC-1, SPEC-2",
        "spec-items: SPEC-1",
    )

    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="Apply both requirements.",
    ) == ["spec-items: expected SPEC-1, SPEC-2 (declared SPEC-1)"]

def test_source_spec_items_reject_duplicate_ids_and_invalid_verifier(tmp_path):
    artifact = """## Spec Items

SPEC-1: First requirement.
verify: proof:artifact-text
SPEC-1: Duplicate requirement.
verify: manual

## Completion Gate

spec-items: SPEC-1
"""

    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "prd",
        artifact,
        task_text="Apply both requirements.",
    ) == [
        "SPEC-1: verify: test:<name> | symbol:<symbol>=<value> | manual "
        "(invalid verifier: proof:artifact-text)",
        "SPEC-1: duplicate SPEC id",
    ]

def test_nonempty_task_rejects_spec_items_none(tmp_path):
    artifact = "## Spec Items\n\n## Completion Gate\n\nspec-items: none\n"

    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="Show an empty state.",
    ) == [
        "spec-items: at least one SPEC item "
        "(the run task contains user instructions; 'none' is not allowed)"
    ]
    assert design_value_check.missing_spec_item_evidence(
        tmp_path,
        tmp_path,
        "design",
        artifact,
        task_text="",
    ) == []

def test_capture_ignores_non_source_phases(tmp_path):
    assert capture_design_ledger(tmp_path, "implement", ARTIFACT) is None
    assert not (tmp_path / LEDGER_FILE).exists()


def test_no_ledger_yields_no_block(tmp_path):
    assert ledger_prompt_block(tmp_path) == ""


def test_missing_ledger_after_source_artifact_fails_closed(tmp_path):
    (tmp_path / "design.md").write_text(SPEC_ARTIFACT, encoding="utf-8")

    with pytest.raises(RuntimeError, match="design-spec.md is missing"):
        ledger_prompt_block(tmp_path)


def test_empty_ledger_still_injects(tmp_path):
    """반증: 빈 원장을 침묵으로 접으면 '수치가 없다'와 '기록을 안 했다'가 같아진다."""
    _capture(tmp_path, "design", "## Design Values\n\n")
    block = ledger_prompt_block(tmp_path)
    assert "No design values were recorded" in block


def _write_task_meta(run_dir: Path, task: str, *, digest: str | None = None) -> None:
    payload = {"task": task}
    payload["task_digest"] = (
        digest
        if digest is not None
        else hashlib.sha256(task.strip().encode("utf-8")).hexdigest()
    )
    (run_dir / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def test_task_swap_after_capture_invalidates_the_ledger(tmp_path):
    """반증: run 시작 뒤 task를 비우면 SPEC 없는 원장이 정당해진다."""
    _write_task_meta(tmp_path, "Add an empty state for search results.")
    _capture(tmp_path, "design", SPEC_ARTIFACT)

    _write_task_meta(tmp_path, "")

    assert "run task does not match design-spec.md" in read_ledger(tmp_path).errors
    with pytest.raises(RuntimeError, match="run task does not match"):
        ledger_prompt_block(tmp_path)


def test_metadata_task_digest_must_match_its_own_task(tmp_path):
    _write_task_meta(tmp_path, "Add an empty state.", digest="0" * 64)

    with pytest.raises(ValueError, match="task digest does not match task"):
        _capture(tmp_path, "design", SPEC_ARTIFACT)


def test_metadata_copies_must_agree_on_the_task(tmp_path):
    _write_task_meta(tmp_path, "Add an empty state.")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "task": "Something else entirely.",
                "task_digest": hashlib.sha256(
                    b"Something else entirely."
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="task differs between metadata files"):
        _capture(tmp_path, "design", SPEC_ARTIFACT)


def _envelope(tmp_path: Path, phase_id: str) -> str:
    adapter = GenericAdapter()
    adapter._task_text = "add the login screen"
    return adapter.render_envelope(
        Phase(id=phase_id, description="d", prompt="p"),
        run_dir=tmp_path,
        project_root=tmp_path,
    )


@pytest.mark.parametrize("phase_id", ["red", "green", "refactor", "multi-review", "gates", "commit"])
def test_every_phase_envelope_carries_the_ledger(tmp_path, phase_id):
    """#102 F2의 핵심. 수치가 살아남는 phase가 0개였다."""
    _capture(tmp_path, "prd", ARTIFACT)
    envelope = _envelope(tmp_path, phase_id)
    assert "horizontal-padding" in envelope
    assert "16dp" in envelope
    assert "#FF6B00" in envelope


def test_envelope_carries_the_task_text(tmp_path):
    """hosted 어댑터 엔벨로프에는 task 텍스트조차 없었다."""
    assert "add the login screen" in _envelope(tmp_path, "red")


def test_none_marker_contradicting_recorded_values_is_blocked():
    """반증: `design-values: none` 한 줄로 수치 기록 전체를 건너뛸 수 있으면 안 된다."""
    text = "## Design Values\n\ngap: 8dp\n\n## Completion Gate\n\ndesign-values: none\n"
    missing = missing_design_value_markers(text, "prd")
    assert any(v.startswith("design-values: gap") for v in missing)


def test_values_marker_without_a_section_is_blocked():
    text = "## Design Values\n\n\n## Completion Gate\n\ndesign-values: gap\n"
    assert missing_design_value_markers(text, "prd") == [
        "design-values: none (the ## Design Values section is empty)"
    ]


def test_confirmation_escape_hatch_is_blocked_when_values_exist():
    """반증: `n/a`가 즉시 기본값이 되면 사람 관측자가 소멸한다."""
    text = (
        "## Design Values\n\ngap: 8dp\n\n## Completion Gate\n\n"
        "design-values: gap\ndesign-values-confirmed: n/a\n"
    )
    missing = missing_design_value_markers(text, "prd")
    assert any("design-values-confirmed: yes" in v for v in missing)


def test_confirmation_na_is_fine_with_no_values():
    text = "## Design Values\n\n\n## Completion Gate\n\ndesign-values: none\ndesign-values-confirmed: n/a\n"
    assert missing_design_value_markers(text, "prd") == []


def test_consistent_artifact_passes():
    assert missing_design_value_markers(ARTIFACT, "prd") == []


def test_non_source_phases_are_not_checked():
    text = "## Completion Gate\n\ndesign-values: none\n"
    assert missing_design_value_markers(text, "implement") == []


@pytest.mark.parametrize("copy", ["src/agent_flow/workflows"])
def test_prd_pauses_for_the_read_back(copy):
    """관측자가 사람인 유일한 지점이다. pause가 없으면 확인 자리가 없다."""
    import yaml

    workflow = yaml.safe_load((REPO / copy / "full-feature.yaml").read_text(encoding="utf-8"))
    prd = next(phase for phase in workflow["phases"] if phase["id"] == "prd")
    assert prd["pause_after"] is True
    assert "design-values-confirmed: yes|n/a" in prd["required_markers"]


@pytest.mark.parametrize(
    ("copy", "workflow_name", "phase_id"),
    [
        ("src/agent_flow/workflows", "default.yaml", "design"),
        ("src/agent_flow/workflows", "full-feature.yaml", "prd"),
    ],
)
def test_default_and_full_feature_require_spec_items(copy, workflow_name, phase_id):
    import yaml

    workflow = yaml.safe_load((REPO / copy / workflow_name).read_text(encoding="utf-8"))
    phase = next(item for item in workflow["phases"] if item["id"] == phase_id)

    assert "## Spec Items" in phase["required_markers"]
    assert "spec-items:" in phase["required_markers"]
    assert "SPEC-<n>" in phase["prompt"]
    assert "test:<name> | symbol:<symbol>=<value> | manual" in phase["prompt"]


def test_parses_declared_concerns_from_the_completion_gate():
    """경로가 드러내지 못하는 concern의 유일한 무료 입력 경로다. 소문자로 눕히고
    쉼표로 나누며, `none`과 부재는 같은 뜻이다."""
    gate = ARTIFACT.replace(
        "design-values-confirmed: yes",
        "design-values-confirmed: yes\nconcerns: Architecture, rsc-boundaries",
    )
    assert parse_declared_concerns(gate) == ("architecture", "rsc-boundaries")
    assert parse_declared_concerns(ARTIFACT) == ()
    assert parse_declared_concerns(
        ARTIFACT.replace("design-values-confirmed: yes", "concerns: none")
    ) == ()


def test_declared_concerns_ignores_code_blocks():
    """예시 줄로 required를 키우면 문서에 마커를 쓴 것만으로 기준이 바뀐다."""
    assert parse_declared_concerns(
        "## Completion Gate\n\n```\nconcerns: architecture\n```\n"
    ) == ()
