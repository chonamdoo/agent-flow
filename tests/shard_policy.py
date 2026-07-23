from __future__ import annotations

from pathlib import Path


SHARDS = ("fast", "integration", "parity", "worktree-lifecycle")

_CUSTOM_PARITY_TESTS = {
    "test_parity_checker_validates_external_installed_copy_from_managed_source_worktree",
}

_WORKTREE_TERMS = (
    "branch",
    "cleanup",
    "compare_and_delete",
    "finalizer",
    "protected_branch",
    "quarantine",
    "reference_hook",
    "reference_transaction",
    "stale_manifest",
    "stale_worktree",
    "worktree",
)

_CLI_INTEGRATION_TERMS = (
    "export",
    "install",
    "installer",
    "packaged_resources",
)

_CLI_INTEGRATION_PREFIXES = (
    "test_agent_doc_block_",
    "test_legacy_node_",
    "test_node_",
)

CLI_CACHED_ROUTING_TESTS = {
    "test_node_architecture_review_request_changes_routes_to_refactor",
    "test_node_code_phase_requires_runtime_selected_skill_contract",
    "test_node_default_final_review_uses_multi_review_rules",
    "test_node_fix_loop_round_cap_blocks_after_max",
    "test_node_gates_fail_routes_to_fix_loop_and_back",
    "test_node_heading_required_markers_ignore_fenced_examples",
    "test_node_multi_review_request_changes_routes_to_fix_loop",
    "test_node_multi_review_requires_subagent_reviewer",
    "test_node_multi_review_single_request_changes_routes_to_fix_loop",
    "test_node_plan_review_and_architecture_review_route_request_changes",
    "test_node_pr_watch_blocks_pending_and_routes_fix_loops_back_to_watch",
    "test_node_push_watch_blocks_detached_head",
    "test_node_push_watch_blocks_protected_branches",
    "test_node_push_watch_tick_blocks_before_pr_watch_phase",
    "test_node_push_watch_tick_records_failed_ci_status",
    "test_node_push_watch_tick_requires_review_approval_before_green",
    "test_node_push_watch_tick_treats_legacy_failed_context_as_ci_failed",
    "test_node_push_watch_tick_treats_legacy_success_context_as_green_when_approved",
    "test_node_routing_template_clones_are_isolated",
    "test_node_status_escapes_task_newlines_and_emits_json",
    "test_node_workflow_run_advances_all_phases_and_handles_complete_state",
    "test_node_workflow_run_blocks_phase_skip_until_artifact_exists",
    "test_node_workflow_run_normalizes_persisted_phase_index_from_phase_name",
}


def shard_for_test(path: str | Path, test_name: str) -> str:
    filename = Path(path).name
    normalized = test_name.split("[", 1)[0]

    if filename == "test_skill_runtime_parity.py":
        return "parity"
    if filename == "test_custom_skill_install.py":
        if normalized in _CUSTOM_PARITY_TESTS:
            return "parity"
        return "integration"
    if filename == "test_pinned_workspace_boundary.py":
        if _contains_any(normalized, _WORKTREE_TERMS):
            return "worktree-lifecycle"
        return "integration"
    if filename == "test_runner_smoke.py" and _contains_any(normalized, _WORKTREE_TERMS):
        return "worktree-lifecycle"
    if filename == "test_cli.py":
        if normalized in CLI_CACHED_ROUTING_TESTS:
            return "integration"
        if _contains_any(normalized, _WORKTREE_TERMS):
            return "worktree-lifecycle"
        if normalized.startswith(_CLI_INTEGRATION_PREFIXES) or _contains_any(normalized, _CLI_INTEGRATION_TERMS):
            return "integration"
    return "fast"


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(term in value for term in terms)
