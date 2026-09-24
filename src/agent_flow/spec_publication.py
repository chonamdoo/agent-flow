from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from agent_flow.core.artifacts import read_deferred_ci_checks
from agent_flow.core.delivery_evidence import missing_delivery_evidence, parse_delivery_fields
from agent_flow.core.design_ledger import SpecPublicationEvidence
from agent_flow.core.gate_plan import deferred_check_names, profile_gate_commands
from agent_flow.core.phase_workflow import find_kit_root
from agent_flow.core.profile_resolution import resolve_profile
from agent_flow.core.workflow_pin import load_run_workflow_definition
from agent_flow.core.worktree_isolation import git_safe, resolve_run_subpath
from agent_flow.pr_watch import fetch_pr


def observe_spec_publication(
    project_root: Path, run_dir: Path, *, profile: dict | None = None,
    post_merge: bool = False,
    config_root: Path | None = None,
) -> SpecPublicationEvidence:
    artifact = next((
        path for path in (run_dir / "push-pr.md", run_dir / "artifacts/push-pr.md")
        if path.is_file()
    ), None)
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict) and (meta.get("workflow") or "workflow_definition" in meta):
        try:
            definition = load_run_workflow_definition(
                find_kit_root(), str(meta.get("workflow", "")), meta,
                config_root=config_root or project_root,
            )
            phase = next((item for item in definition.phases if item.id == "push-pr"), None)
            if phase is None:
                return SpecPublicationEvidence(missing=(
                    "pre-merge SPEC: workflow has no push-pr publication phase",
                ))
            canonical = resolve_run_subpath(run_dir, Path(phase.artifact))
            if canonical.is_file():
                artifact = canonical
        except (OSError, ValueError, RuntimeError) as exc:
            return SpecPublicationEvidence(missing=(
                f"pre-merge SPEC: publication contract is unavailable: {exc}",
            ))
    if artifact is None:
        return SpecPublicationEvidence(missing=(
            "pre-merge SPEC: push-pr publication evidence is missing",
        ))
    try:
        text = artifact.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return SpecPublicationEvidence(missing=(
            "pre-merge SPEC: push-pr publication evidence is unreadable",
        ))
    missing = missing_delivery_evidence(
        project_root, "push-pr", text, profile=profile, post_merge=post_merge,
    )
    if missing:
        return SpecPublicationEvidence(missing=tuple(missing))
    fields, missing = parse_delivery_fields(text, ("pr-url", "remote-oid"))
    if missing:
        return SpecPublicationEvidence(missing=tuple(missing))
    url = urlsplit(fields["pr-url"])
    match = re.fullmatch(r"/([^/]+/[^/]+)/pull/([1-9]\d*)/?", url.path)
    if not url.netloc or match is None:
        return SpecPublicationEvidence(missing=(
            "pre-merge SPEC: cannot resolve published PR identity",
        ))
    try:
        active_profile = profile
        if not active_profile or not active_profile.get("active_profiles"):
            _, active_profile = resolve_profile(
                find_kit_root(), config_root or project_root,
            )
        profile_ids = active_profile.get("active_profiles") or [active_profile["id"]]
        configured_checks = deferred_check_names(profile_gate_commands(
            profile_ids, root=config_root or project_root, phase="all", execution="ci",
        ))
        required_checks = tuple(dict.fromkeys((
            *configured_checks, *read_deferred_ci_checks(run_dir),
        )))
    except (OSError, UnicodeError, ValueError) as exc:
        return SpecPublicationEvidence(missing=(
            f"pre-merge SPEC: cannot verify required CI gates: {exc}",
        ))
    # ACK한 feedback은 pr-watch와 같이 제외하되, 이 관측은 runner lease 안에서도
    # 불리므로 run 상태를 기록하지 않는다.
    snapshot = fetch_pr(
        int(match.group(2)), repo=f"{url.netloc}/{match.group(1)}",
        required_checks=required_checks, require_ready=True,
        run_dir=run_dir, record_feedback=False,
    )
    if snapshot.status == "error":
        return SpecPublicationEvidence(missing=(
            f"pre-merge SPEC: PR observation failed: {snapshot.error}",
        ))
    current = git_safe(
        "rev-parse", "--verify", "HEAD^{commit}", cwd=project_root, optional_locks=False,
    )
    head = fields["remote-oid"].lower()
    if (
        snapshot.head.lower() != head
        or not current.ok or current.stdout.strip().lower() != head
        or not re.fullmatch(r"[0-9a-f]{40,64}", head)
    ):
        return SpecPublicationEvidence(missing=(
            "pre-merge SPEC: CI observation does not match current publication HEAD",
        ))
    expected_status = "merged" if post_merge else "green"
    if snapshot.status != expected_status:
        return SpecPublicationEvidence(missing=(
            f"pre-merge SPEC: expected {expected_status} publication ({snapshot.status})",
        ))
    return SpecPublicationEvidence(head=head)
