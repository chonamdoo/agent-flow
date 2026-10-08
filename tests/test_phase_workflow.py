from pathlib import Path, PureWindowsPath
from typing import Any, get_args

import pytest

from agent_flow.core.phase_workflow import (
    load_phase_workflow_definition,
    workflow_names,
)
from agent_flow.core.review_evidence import ReviewVerdict
from agent_flow.core.route_verdicts import (
    ARTIFACT_ROUTE_KEYS,
    GATE_FAIL_STATUSES,
    GATE_PASS_STATUSES,
)
from agent_flow.runner import Phase, Runner, _phases_from_definition


def test_unknown_completion_disposition_cannot_fall_back_to_cleanup(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "custom.yaml").write_text(
        "id: custom\ncompletion_disposition: local-hanoff\nphases:\n  - id: implement\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="completion_disposition"):
        load_phase_workflow_definition(tmp_path, "custom")


def test_custom_workflow_cannot_declare_bundled_replacement_authority(tmp_path: Path) -> None:
    """Verify that custom workflow cannot declare bundled replacement authority."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n"
        "      replaceable_architecture: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown keys.*replaceable_architecture"):
        load_phase_workflow_definition(tmp_path, "default")


@pytest.mark.parametrize("name", ["custom", "default"])
@pytest.mark.parametrize("alias_installed", [False, True])
def test_obsolete_required_alias_requires_explicit_migration(
    tmp_path: Path, name: str, alias_installed: bool
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    source = (
        f"id: {name}\nphases:\n  - id: implement\n"
        "    skills:\n      required: [clean-architecture]\n"
    )
    path = workflows / f"{name}.yaml"
    path.write_text(source, encoding="utf-8")
    if alias_installed:
        alias = tmp_path / "skills" / "clean-architecture"
        alias.mkdir(parents=True)
        (alias / "SKILL.md").write_text(
            "---\nname: clean-architecture\n---\nCompatibility alias.\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match=r"migration required.*clean-architecture-core"):
        load_phase_workflow_definition(tmp_path, name)
    assert path.read_text(encoding="utf-8") == source


def test_custom_canonical_consumer_has_no_kit_replacement_authority(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n",
        encoding="utf-8",
    )

    definition = load_phase_workflow_definition(tmp_path, "default")

    skills = definition.phases[0].skills
    assert skills is not None
    assert skills.required == ("clean-architecture-core",)
    assert not skills.replaceable_architecture


def test_bundled_copy_retains_kit_replacement_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "package" / "workflows" / "default.yaml"
    packaged.parent.mkdir(parents=True)
    source = (
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n"
    )
    packaged.write_text(source, encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        "agent_flow.core.phase_workflow._packaged_workflow_path", lambda name: packaged
    )

    definition = load_phase_workflow_definition(tmp_path, "default")

    skills = definition.phases[0].skills
    assert skills is not None
    assert skills.replaceable_architecture


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
def test_conditional_markers_enforce_only_selected_architecture(mode: str) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import (
        effective_phase_markers,
        parse_phase_workflow_definition,
    )

    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: review\n"
        b"    required_markers: ['architecture-contract: applied']\n"
        b"    required_markers_by_architecture:\n"
        b"      clean: ['repository-boundary: pass|fail']\n"
        b"      local: ['local-boundary: pass|fail']\n"
        b"      pending: ['existing-pattern: checked']\n",
        source=Path("custom.yaml"),
        name="custom",
    )
    markers = effective_phase_markers(definition.phases[0], mode)
    evidence = "## Completion Gate\narchitecture-contract: applied\n"
    expected = {
        "clean": ("repository-boundary: pass|fail", "repository-boundary: pass"),
        "local": ("local-boundary: pass|fail", "local-boundary: pass"),
        "pending": ("existing-pattern: checked", "existing-pattern: checked"),
    }
    requirement, completion = expected[mode]
    assert missing_markers(evidence, markers) == [requirement]
    assert missing_markers(evidence + completion + "\n", markers) == []


@pytest.mark.parametrize(
    "conditional",
    [
        None,
        [],
        "clean",
        {"unknown": []},
        {1: []},
        {"clean": None},
        {"local": "marker: applied"},
        {"pending": [False]},
        {"clean": [""]},
        {"clean": ["   "]},
        {"clean": [{"marker": "applied"}]},
    ],
)
def test_malformed_conditional_marker_schema_is_rejected(conditional: object) -> None:
    import yaml
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition

    source = yaml.safe_dump({
        "id": "custom",
        "phases": [{
            "id": "review",
            "required_markers_by_architecture": conditional,
        }],
    }).encode()
    with pytest.raises(ValueError, match="required_markers_by_architecture"):
        parse_phase_workflow_definition(source, source=Path("custom.yaml"), name="custom")


def test_absent_conditional_field_preserves_legacy_export_and_source_digest() -> None:
    import hashlib
    from agent_flow.core.phase_workflow import (
        effective_phase_markers,
        parse_phase_workflow_definition,
    )

    source = b"id: custom\nphases:\n  - id: review\n    required_markers: ['clean-architecture: applied|n/a']\n"
    definition = parse_phase_workflow_definition(
        source, source=Path("custom.yaml"), name="custom", pinned_legacy=True
    )
    assert definition.to_json_dict() == {
        "id": "custom",
        "source": "custom.yaml",
        "digest": hashlib.sha256(source).hexdigest(),
        "completion_disposition": "integrated-cleanup",
        "phases": [{
            "id": "review", "description": "", "prompt": None,
            "pause_after": False, "optional": False, "multi_review": False,
            "routes": None,
            "required_markers": ("clean-architecture: applied|n/a",),
            "artifact": "review.md", "skills": None, "architecture_decision": "existing",
        }],
    }
    for mode in ("clean", "local", "pending"):
        assert effective_phase_markers(definition.phases[0], mode) == (
            "clean-architecture: applied|n/a",
        )


def test_explicit_empty_conditional_field_is_preserved_in_export() -> None:
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition

    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: review\n    required_markers_by_architecture: {}\n",
        source=Path("custom.yaml"), name="custom",
    )
    assert definition.to_json_dict()["phases"][0]["required_markers_by_architecture"] == {}


def test_stack_mode_markers_are_accepted_and_applied() -> None:
    from agent_flow.core.phase_workflow import effective_phase_markers, parse_phase_workflow_definition

    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: review\n    required_markers_by_architecture:\n"
        b"      stack: ['stack-contract: pass|fail']\n",
        source=Path("custom.yaml"), name="custom",
    )

    assert effective_phase_markers(definition.phases[0], "stack") == ("stack-contract: pass|fail",)
    assert effective_phase_markers(definition.phases[0], "clean") == ()


@pytest.mark.parametrize("workflow,phase_id", [
    ("default", "design"), ("full-feature", "ddd-design"),
])
def test_fresh_design_requires_clean_boundary_evidence_only_in_clean_mode(
    workflow: str, phase_id: str
) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import effective_phase_markers

    definition = load_phase_workflow_definition(Path(__file__).resolve().parents[1], workflow)
    phase = next(phase for phase in definition.phases if phase.id == phase_id)
    evidence = (
        "## Architecture Boundary Map\nSelected contract ownership evidence.\n"
        "## Composition Root\nExplicit construction.\n"
        "## Testability Boundary\nConsumer seam.\n"
        "## Spec Items\nSPEC-1: preserve boundaries\n"
        "## Design Values\n"
        "## Completion Gate\n"
        "architecture-contract: applied\n"
        "solid-srp-change-reason: ownership\n"
        "solid-ocp-extension-points: stable ports\n"
        "solid-lsp-contracts: preserved\n"
        "solid-isp-consumer-ports: narrow\n"
        "solid-dip-dependency-direction: inward\n"
        "spec-items: SPEC-1\n"
        "design-values: none\n"
    )
    assert missing_markers(evidence, effective_phase_markers(phase, "local")) == []
    assert missing_markers(evidence, effective_phase_markers(phase, "pending")) == []
    assert missing_markers(evidence, effective_phase_markers(phase, "clean")) == [
        "## Dependency Rule",
        "## Use Case Boundaries",
        "usecase-interface: required|optional|n/a",
        "usecase-composition: none|domain-service|application-service|orchestrator|justified",
        "## Repository Boundaries",
        "## Cache Boundary",
        "cache-required: yes|no",
        "memory-cache: required|optional|n/a",
        "disk-cache: required|optional|n/a",
        "cache-invalidation-policy:",
        "## Mapping Boundary",
        "remote-dto-domain-mapper: required|optional|n/a",
        "entity-domain-mapper: required|optional|n/a",
        "domain-ui-mapper: required|optional|n/a",
    ]


@pytest.mark.parametrize("workflow,phase_id", [
    ("default", "final-review"), ("full-feature", "architecture-review"),
])
def test_fresh_clean_review_preserves_boundary_exceptions(
    workflow: str, phase_id: str
) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import effective_phase_markers

    definition = load_phase_workflow_definition(Path(__file__).resolve().parents[1], workflow)
    phase = next(phase for phase in definition.phases if phase.id == phase_id)
    markers = effective_phase_markers(phase, "clean")
    evidence = "## Completion Gate\nusecase-boundary: n/a\nusecase-calls-usecase: n/a\n"
    missing = missing_markers(evidence, markers)
    assert "usecase-boundary: pass|fail|n/a" not in missing
    assert "usecase-calls-usecase: pass|fail|n/a" not in missing
    assert "repository-boundary: pass|fail" in missing
    assert "repository-boundary: pass|fail" in missing_markers(
        evidence + "repository-boundary: n/a\n", markers
    )
    assert "repository-boundary: pass|fail" not in missing_markers(
        evidence + "repository-boundary: pass\n", markers
    )


def test_edited_workflow_file_is_parsed_again(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    path = workflows / "custom.yaml"
    path.write_text("id: custom\nphases:\n  - id: implement\n", encoding="utf-8")
    assert load_phase_workflow_definition(tmp_path, "custom").phases[0].artifact == "implement.md"

    path.write_text(
        "id: custom\nphases:\n  - id: implement\n    artifact: out/implement.md\n", encoding="utf-8",
    )

    assert load_phase_workflow_definition(tmp_path, "custom").phases[0].artifact == "out/implement.md"


@pytest.mark.parametrize("first_change,second_change", [
    ({}, {"source": Path("kit/custom.yaml")}),
    ({}, {"name": "other"}),
    ({}, {"kit_owned": True}),
    ({}, {"pinned_legacy": True}),
    # Windows 경로 비교는 대소문자를 무시하지만 기록되는 source 문자열은 다르다.
    ({"source": PureWindowsPath("C:/Proj/custom.yaml")}, {"source": PureWindowsPath("c:/proj/custom.yaml")}),
])
def test_same_bytes_keep_each_callers_identity_and_authority(
    first_change: dict[str, Any], second_change: dict[str, Any],
) -> None:
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition

    source = b"phases:\n  - id: implement\n    skills:\n      required: [clean-architecture-core]\n"
    base: dict[str, Any] = {
        "source": Path("custom.yaml"), "name": "custom", "kit_owned": False, "pinned_legacy": False,
    }
    second = {**base, **second_change}
    parse_phase_workflow_definition(source, **{**base, **first_change})

    definition = parse_phase_workflow_definition(source, **second)

    skills = definition.phases[0].skills
    assert skills is not None
    assert (
        definition.source, definition.id, definition.kit_owned,
        skills.replaceable_architecture, skills.pinned_legacy,
    ) == (
        str(second["source"]), second["name"], second["kit_owned"],
        second["kit_owned"], second["pinned_legacy"],
    )


def test_changing_a_loaded_definition_does_not_leak_into_later_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    first = load_phase_workflow_definition(root, "full-feature")
    phase = next(phase for phase in first.phases if phase.routes)
    assert phase.routes is not None
    original = dict(phase.routes)
    phase.routes.clear()

    again = load_phase_workflow_definition(root, "full-feature")

    assert next(item for item in again.phases if item.id == phase.id).routes == original


_ROOT = Path(__file__).resolve().parents[1]
# PR 이벤트 루프는 리뷰 코멘트와 CI 결과 같은 외부 이벤트로만 다시 돈다. 그래서 fix-loop 라운드 상한
# 대상이 아니다(CI 수리 횟수는 `CI_REPAIR_MAX_ROUNDS`가 따로 막는다). 예외는 그 이벤트로 `pr-watch`를
# 떠나는 edge뿐이고, `pr-watch`를 지나는 다른 순환은 그대로 검사한다.
_PR_EVENT_LOOP_PHASE = "pr-watch"
_PR_EVENT_ROUTE_KEYS = frozenset({"comments", "has_comments", "ci-failed", "ci_failed"})
# route 없는 phase가 다음 자리로 넘어가는 edge의 key. runner가 이 이동에 붙이는 key와 같다.
_FALLTHROUGH = "none"


def _emittable_route_keys(phase: Phase) -> frozenset[str]:
    """runner(`_next_index`)가 이 phase의 artifact에서 읽어 낼 수 있는 route key.

    multi-review의 증거 실패 key는 route를 찾기 전에 제자리에서 막히므로 넣지 않는다.
    """
    if phase.multi_review:
        return frozenset(get_args(ReviewVerdict))
    if phase.id == "gates":
        return GATE_PASS_STATUSES | GATE_FAIL_STATUSES | {"default"}
    return ARTIFACT_ROUTE_KEYS | {"default"}


def _route_edges(phases: list[Phase]) -> dict[int, dict[str, int]]:
    """phase마다 읽어 낼 수 있는 route key → 다음 자리.

    runner처럼 선언되지 않은 key는 `default`로 가고, `block`이거나 갈 곳이 없으면 제자리에서
    멈추므로 edge가 없다. 코드가 바뀐 PR fix를 첫 review phase로 돌리는 runtime 전용 edge는
    YAML에 없어서 넣지 않는다. 그 edge가 만드는 순환은 모두 PR 이벤트 edge를 지나므로 판정은 같다.
    """
    index = {phase.id: position for position, phase in enumerate(phases)}
    edges: dict[int, dict[str, int]] = {}
    for position, phase in enumerate(phases):
        if not phase.routes:
            edges[position] = {_FALLTHROUGH: position + 1}
            continue
        edges[position] = {}
        for key in _emittable_route_keys(phase):
            target = phase.routes.get(key)
            if target is None:
                target = phase.routes.get("default")
            if target is not None and target != "block":
                edges[position][key] = index[target]
    return edges


def _route_graph(phases: list[Phase]) -> dict[int, set[int]]:
    return {position: set(by_key.values()) for position, by_key in _route_edges(phases).items()}


def _unemittable_route_keys(phases: list[Phase]) -> list[tuple[str, str]]:
    """선언됐지만 runner가 읽어 낼 수 없어 한 번도 쓰이지 않는 route key."""
    return [
        (phase.id, key)
        for phase in phases
        for key in phase.routes or {}
        if key != "default" and key not in _emittable_route_keys(phase)
    ]


def _reachable(graph: dict[int, set[int]], start: int) -> set[int]:
    seen = {start}
    pending = [start]
    while pending:
        for successor in graph.get(pending.pop(), ()):
            if successor not in seen:
                seen.add(successor)
                pending.append(successor)
    return seen


def _uncapped_cycle_phases(phases: list[Phase]) -> list[str]:
    """상한에 걸리지 않고 반복될 수 있는 순환 위의 phase.

    runner는 route로 fix collector에 들어갈 때만 상한을 검사한다. route 없는 phase에서 넘어가는
    진입은 라운드를 기록해도 막히지 않으므로 그 edge는 끊지 않는다.
    """
    runner = object.__new__(Runner)
    runner.phases = phases
    collectors = runner._fix_collector_targets()
    graph = {
        position: {
            target
            for key, target in by_key.items()
            if target < len(phases)
            and not (key != _FALLTHROUGH and phases[target].id in collectors)
            and not (phases[position].id == _PR_EVENT_LOOP_PHASE and key in _PR_EVENT_ROUTE_KEYS)
        }
        for position, by_key in _route_edges(phases).items()
    }
    return [
        phases[position].id
        for position, successors in graph.items()
        if any(position in _reachable(graph, successor) for successor in successors)
    ]


@pytest.mark.parametrize("name", workflow_names(_ROOT))
def test_every_packaged_workflow_reaches_completion_and_bounds_its_rework_cycles(name: str) -> None:
    """반증: route 하나를 잘못 고치면 그 key가 쓰이지 않거나, phase가 조용히 skip되거나, 완료로
    가는 길이 끊기거나, 재작업 순환이 상한 없이 반복된다."""
    phases = _phases_from_definition(load_phase_workflow_definition(_ROOT, name))
    graph = _route_graph(phases)
    completion = len(phases)

    assert _unemittable_route_keys(phases) == []
    reachable = _reachable(graph, 0)
    assert [phase.id for position, phase in enumerate(phases) if position not in reachable] == []
    assert [
        phase.id
        for position, phase in enumerate(phases)
        if completion not in _reachable(graph, position)
    ] == []
    assert _uncapped_cycle_phases(phases) == []


def test_a_rework_cycle_is_capped_only_by_a_routed_entry_into_a_fix_collector() -> None:
    """반증: 순환 판정이 늘 빈 목록을 돌려주면 위 불변식은 아무것도 지키지 못하고,
    runner가 막지 않는 진입까지 상한으로 치면 실제로 끝나지 않는 순환을 놓친다."""
    implement = Phase(id="implement", description="")
    handoff = Phase(id="handoff", description="")
    by_comment = Phase(id="review", description="", routes={"approve": "handoff", "comments": "implement"})
    by_rejection = Phase(
        id="review", description="", routes={"approve": "handoff", "request-changes": "implement"}
    )

    assert _uncapped_cycle_phases([implement, by_comment, handoff]) == ["implement", "review"]
    assert _uncapped_cycle_phases([implement, by_rejection, handoff]) == []

    # `fix`는 collector지만 이 순환은 route 없는 `prep`에서 넘어가며 들어가므로 상한 검사를 받지 않는다.
    triage = Phase(
        id="review",
        description="",
        routes={"approve": "handoff", "request-changes": "fix", "comments": "prep"},
    )
    prep = Phase(id="prep", description="")
    fix = Phase(id="fix", description="", routes={"default": "review"})
    assert _uncapped_cycle_phases([triage, prep, fix, handoff]) == ["review", "prep", "fix"]


def test_the_pr_event_loop_exemption_does_not_hide_other_cycles_through_pr_watch() -> None:
    """반증: `pr-watch`를 통째로 빼면 그 phase를 지나는 잘못된 역방향 route까지 상한 검사에서 사라진다."""
    commit = Phase(id="commit", description="")
    comment_fix = Phase(id="pr-comment-fix", description="", routes={"default": "pr-watch"})
    handoff = Phase(id="handoff", description="")
    watch = Phase(
        id="pr-watch", description="", routes={"green": "handoff", "comments": "pr-comment-fix"}
    )
    watch_back_to_commit = Phase(
        id="pr-watch", description="", routes={"green": "commit", "comments": "pr-comment-fix"}
    )

    assert _uncapped_cycle_phases([commit, watch, comment_fix, handoff]) == []
    assert _uncapped_cycle_phases([commit, watch_back_to_commit, comment_fix, handoff]) == [
        "commit",
        "pr-watch",
    ]


def test_a_route_key_the_phase_cannot_emit_is_not_an_edge() -> None:
    """반증: 선언된 key를 모두 edge로 치면 오타 난 route가 도달 가능성을 거짓으로 채운다."""
    review = Phase(
        id="review",
        description="",
        multi_review=True,
        routes={"approev": "handoff", "request-changes": "fix"},
    )
    fix = Phase(id="fix", description="", routes={"default": "review"})
    handoff = Phase(id="handoff", description="")
    phases = [review, fix, handoff]

    assert _unemittable_route_keys(phases) == [("review", "approev")]
    assert 2 not in _reachable(_route_graph(phases), 0)
