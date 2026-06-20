"""Hosted adapter — one class, parameterized by host.

Replaces the previous Claude / Codex subclass pair. Each host
contributes only:
  - a name (claude / codex)
  - a host-specific hint string

Real behavior divergence (multi-reviewer fan-out, parallel sub-agents) is
driven by the workflow YAML's per-phase `multi_review: true` flag, not by
adapter subclass. This kills the copy-paste polymorphism that the architectural
review flagged.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from types import MappingProxyType

from agent_flow.adapters.base import Adapter
from agent_flow.multi_review import (
    ReviewerJob,
    Distribution,
    distribute,
    residual_host_jobs,
    resolve_review_clis,
    run_distribution,
)

_BASE_REVIEW_ANGLES: tuple[dict[str, str], ...] = (
    {
        "id": "generalist",
        "prompt": "templates/_shared/review/architecture.md",
    },
    {
        "id": "architecture-design",
        "prompt": "templates/_shared/review/architecture-design.md",
    },
    {
        "id": "clean-architecture",
        "prompt": "templates/_shared/review/clean-architecture.md",
    },
)
_BASE_REVIEW_PROMPTS = {
    str(item["prompt"])
    for item in _BASE_REVIEW_ANGLES
}

_CLAUDE_HINT = """\
- For multi-reviewer phases, use the `Task` tool to spawn at least two
  reviewer sub-agents in the same assistant message so they execute in parallel.
- Each reviewer section must include `reviewer-source: sub-agent`.
- Use `TodoWrite` for slice tracking during `implement` phase. Mark each
  TDD red→green→refactor step in_progress / completed.
- For long-running phases, prefer parallel reads (multiple `Read` calls in
  one message) over sequential.
- Cite file:line references using the `path/to/file:42` format.
"""

_CODEX_HINT = """\
- For multi-reviewer phases, spawn at least two Codex reviewer sub-agents
  in parallel.
- Each reviewer section must include `reviewer-source: sub-agent`.
- After recording each Codex sub-agent result in `final-review.md`, close that
  sub-agent session.
- If agent-flow already distributed angles across installed CLIs, invoke each
  non-host CLI in parallel and aggregate stdout into the artifact.
- Per-angle artifacts are written by agent-flow as `final-review-<angle>.md`
  when subprocess delegation succeeds; aggregate them into the final
  `final-review.md` summary.
- Cite file:line references using the `path/to/file:42` format.
"""

# Read-only mapping. Wrapped to prevent third-party runtime mutation that
# would silently change adapter behavior across the process.
_HOST_HINTS: MappingProxyType[str, str] = MappingProxyType({
    "claude": _CLAUDE_HINT,
    "codex": _CODEX_HINT,
})


class HostedAdapter(Adapter):
    """Single adapter parameterized by host name.

    Construct with `HostedAdapter("claude")` etc. Behavior is identical
    across hosts except the hint block injected into the prompt envelope
    and the multi-reviewer distribution preview.
    """

    def __init__(self, host_name: str) -> None:
        super().__init__()
        if host_name not in _HOST_HINTS:
            raise ValueError(
                f"Unknown host '{host_name}'. Known: {sorted(_HOST_HINTS)}"
            )
        self.name = host_name
        self._hint = _HOST_HINTS[host_name]

    def execute(self, phase, run_dir: Path, project_root: Path) -> bool:
        host_hint = self._hint
        if phase.multi_review:
            distribution = _run_multi_review_distribution(
                phase, run_dir, project_root, self
            )
            host_hint += "\n" + _multi_reviewer_block(distribution)
        prompt = self.render_envelope(
            phase, run_dir, project_root, host_hint=host_hint,
        )
        print(prompt)
        return False  # host AI writes the artifact


def _run_multi_review_distribution(
    phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
) -> Distribution:
    jobs = _reviewer_jobs(phase, run_dir, project_root, adapter)
    distribution = distribute(jobs, host=adapter.name)
    run_distribution(distribution, project_root)
    return distribution


def _reviewer_jobs(
    phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
) -> list[ReviewerJob]:
    profile_angles = adapter._profile_snapshot.get("review_angles") or []
    angles = _merge_review_angles(_BASE_REVIEW_ANGLES, profile_angles)
    jobs: list[ReviewerJob] = []
    base_prompt = adapter.render_envelope(phase, run_dir, project_root)
    for item in angles:
        if not isinstance(item, dict):
            continue
        angle_id = str(item.get("id") or "").strip()
        if not angle_id:
            continue
        angle_prompt = (
            f"{base_prompt}\n\n"
            f"## Review angle\n\n"
            f"- id: {angle_id}\n"
            f"{_review_angle_prompt(project_root, item.get('prompt', ''))}\n"
        )
        jobs.append(ReviewerJob(
            angle_id=angle_id,
            prompt=angle_prompt,
            output_path=run_dir / f"{phase.id}-{angle_id}.md",
        ))
    return jobs


def _merge_review_angles(
    baseline: tuple[dict[str, str], ...],
    profile_angles: object,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {
        str(item["id"]): dict(item)
        for item in baseline
        if item.get("id")
    }
    order = list(merged)
    for item in profile_angles if isinstance(profile_angles, list) else []:
        if not isinstance(item, dict):
            continue
        angle_id = str(item.get("id") or "").strip()
        if not angle_id:
            continue
        if angle_id not in merged:
            order.append(angle_id)
        merged[angle_id] = dict(item)
    return [merged[angle_id] for angle_id in order]


def _review_angle_prompt(project_root: Path, prompt_ref: object) -> str:
    prompt_path = str(prompt_ref or "").strip()
    if not prompt_path:
        raise ValueError("review angle prompt is required")
    _validate_review_prompt_path(prompt_path)
    package_path = resources.files("agent_flow").joinpath(prompt_path)
    repo_path = Path(__file__).resolve().parents[3] / prompt_path
    project_path = project_root / prompt_path
    paths = (
        (package_path, repo_path, project_path)
        if prompt_path in _BASE_REVIEW_PROMPTS
        else (project_path, package_path, repo_path)
    )
    for path in paths:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"review angle prompt not found: {prompt_path}")


def _validate_review_prompt_path(prompt_path: str) -> None:
    path = Path(prompt_path)
    if (
        path.is_absolute()
        or path.parts[:3] != ("templates", "_shared", "review")
        or len(path.parts) != 4
        or not path.parts[-1].endswith(".md")
    ):
        raise ValueError(f"invalid review angle prompt path: {prompt_path}")


def _multi_reviewer_block(distribution: Distribution | None = None) -> str:
    """Distribution preview for multi-reviewer phases.

    The actual angle list is profile-driven and read by the host AI from
    the profile YAML; this block shows the suggested CLI distribution.
    """
    available = resolve_review_clis()
    if not available:
        return ("### Multi-CLI distribution\n"
                "No optional reviewer providers configured. Spawn at least "
                "two host-native reviewer sub-agents, then aggregate their independent "
                "verdicts. "
                "Each reviewer section must include `reviewer-source: sub-agent`. "
                "Close sub-agent sessions after recording results. "
                "Multi-review requires 2+ independent sub-agent reviewer verdicts.\n")
    names = [c.name for c in available]
    lines = [
        "### Multi-CLI distribution",
        f"Configured optional reviewer providers: {', '.join(names)}.",
        "",
        "When fanning out review angles, distribute round-robin across "
        "the installed CLIs (host last). For non-host CLIs, invoke via "
        "the `Bash` tool:",
        "",
    ]
    for cli in available:
        lines.append(f"- `{cli.binaries[0]} {' '.join(cli.invoke)} '<angle prompt>'`")
    lines.append("")
    lines.append(
        "Capture each subprocess's stdout and aggregate into "
        "`final-review.md`. For host-CLI angles, use the host-native "
        "sub-agent mechanism with at least two reviewer sub-agents. "
        "Each reviewer section must include `reviewer-source: sub-agent`. "
        "Close sub-agent sessions after recording results."
    )
    if distribution is not None and distribution.insufficient_reviewers:
        lines.append(
            "Only one reviewer provider is available. Ensure host "
            "sub-agents run so the artifact contains 2+ independent sub-agent "
            "reviewer verdicts."
        )
    if distribution is not None:
        residual = residual_host_jobs(distribution)
        lines.append("")
        lines.append(f"Distribution summary: {distribution.summary()}.")
        if residual:
            lines.append(
                "Residual host-handled angles: "
                + ", ".join(job.angle_id for job in residual)
                + "."
            )
    return "\n".join(lines) + "\n"
