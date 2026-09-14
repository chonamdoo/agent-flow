from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import time


def identity(content: bytes) -> dict:
    """Return the byte length and SHA-256 identity of captured content."""
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def delivered_bytes(prompt: bytes, bodies: list[bytes]) -> dict:
    """Measure exact normative-body delivery and duplicate bytes in a prompt."""
    counts = [(body, prompt.count(body)) for body in dict.fromkeys(bodies) if body]
    return {"raw_input_bytes": len(prompt),
            "delivered_normative_bytes": sum(len(body) * count for body, count in counts),
            "duplicated_bytes": sum(len(body) * max(0, count - 1) for body, count in counts)}


def validate_case_paths(case: dict) -> None:
    """Reject fixture file paths that escape their synthetic project."""
    for name in case["files"]:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError(f"unsafe fixture path: {name}")
    if "references" in case:
        raise ValueError("unsupported fixture field: references; use required_references assertions")
    references = case.get("required_references", [])
    if not isinstance(references, list):
        raise ValueError("required_references must be a list of skills-relative document paths")
    for name in references:
        if not isinstance(name, str):
            raise ValueError(f"unsafe fixture reference: {name!r}")
        relative = PurePosixPath(name)
        if (not relative.parts or relative.is_absolute() or PureWindowsPath(name).drive
                or ".." in relative.parts or "\\" in name):
            raise ValueError(f"unsafe fixture reference: {name!r}")


def document_path(project: Path, path: Path) -> Path:
    """Resolve a selected document while enforcing the project boundary."""
    if not path.resolve().is_relative_to(project.resolve()):
        raise ValueError(f"normative document escapes project: {path}")
    return path


def required_reference_paths(project: Path, case: dict) -> tuple[Path, ...]:
    """Resolve every declared reference required by a fixture case."""
    skills = project / "skills"
    paths = []
    for name in case.get("required_references", []):
        target = document_path(project, skills / name)
        if not target.resolve().is_relative_to(skills.resolve()):
            raise ValueError(f"fixture reference escapes skills: {name}")
        paths.append(target)
    return tuple(paths)


def render(source: Path, project: Path, case: dict, provider: str, output: Path) -> None:
    """Render immutable author, reviewer, and semantic measurement inputs for one case."""
    validate_case_paths(case)
    required_reference_paths(source, case)
    required_reference_paths(project, case)
    sys.path.insert(0, str(source / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.core.phase_workflow import load_phase_workflow_definition
    from agent_flow.core.skill_resolver import PhaseSkills, resolve_phase_skills

    project.mkdir(parents=True)
    shutil.copytree(source / "skills", project / "skills")
    shutil.copytree(source / "templates", project / "templates")
    for name, content in case["files"].items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    mode = case["mode"]
    selection = f"schema_version: 1\narchitecture:\n  mode: {mode}\n"
    if mode == "local":
        selection += "  skill: skills/architecture/SKILL.md\n"
        target = project / "skills/architecture/SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(case["local_contract"], encoding="utf-8")
    (project / ".agent-flow.project.yaml").write_text(selection, encoding="utf-8")
    references = required_reference_paths(project, case)
    definition = load_phase_workflow_definition(source, "default")
    phase = next(item for item in definition.phases if item.id == "final-review")
    declared = phase.skills or PhaseSkills()
    phase = dataclasses.replace(phase, skills=PhaseSkills(
        required=tuple(dict.fromkeys((*declared.required, *case["skills"]))),
        optional=declared.optional, replaceable_architecture=declared.replaceable_architecture,
    ))
    adapter = HostedAdapter(provider)
    adapter._task_text = case["task"]
    adapter._changed_files = tuple(case["files"])
    run_dir = project / ".agent-flow/runs/t6-measurement"
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    resolution = resolve_phase_skills(
        project_root=project, phase_id=phase.id, phase_skills=phase.skills,
        changed_files=adapter._changed_files, task_text=case["task"], host=provider,
    )
    if resolution.missing:
        raise ValueError("missing fixed-corpus required skills: " + ", ".join(s.name for s in resolution.missing))
    documents: dict[str, bytes] = {}
    if hasattr(resolution, "normative_documents"):
        for item in resolution.normative_documents:
            path = document_path(project, project / item.document.path)
            documents[str(path)] = item.content
    else:
        for skill in resolution.available_required:
            if skill.path is not None:
                path = document_path(project, skill.path)
                documents[str(path)] = path.read_bytes()
        for document, content in resolution.architecture_documents:
            path = document_path(project, project / document.path)
            documents[str(path)] = content.encode("utf-8")
    for target in references:
        if str(target) not in documents:
            raise ValueError(f"required reference missing from selected normative capture: {target}")
    if hasattr(resolution, "normative_documents"):
        inline = [(item.document.path, item.content) for item in resolution.normative_documents]
        routes = [dataclasses.asdict(item) for item in resolution.normative_documents]
        for item in routes:
            item.pop("content")
    else:
        inline = [(item.path, content.rstrip().encode("utf-8"))
                  for item, content in resolution.architecture_documents]
        routes = "unavailable: baseline has no all-route delivery metadata"
    author = adapter.render_envelope(phase, run_dir, project, skill_host=provider)
    jobs = _reviewer_jobs(phase, run_dir, project, adapter, providers=(provider,))
    prompts = {"author": author, **{f"reviewer-{job.angle_id}": job.prompt_by_provider[provider] for job in jobs}}
    envelopes = []
    for name, prompt in prompts.items():
        raw = prompt.encode("utf-8")
        (output / f"{name}.prompt.txt").write_bytes(raw)
        envelopes.append({"unit": name, "scope": "rendered-final-envelope-not-provider-call",
                          **identity(raw), **delivered_bytes(raw, [content for _, content in inline]),
                          "call_count": 0})
    normative = []
    semantic_parts = [
        "This is a manual architecture fixture evaluation, not a workflow review or approval. "
        "Review only the supplied change and project decisions against the full selected norms below. "
        "Do not edit files, execute commands, use tools, start or continue any workflow. "
        "Do not manufacture defects from absent scaffolding or preferred naming. "
        "Return only JSON with verdict (approve or request-changes) and findings "
        "(each has file, line, reason). Cite project-relative supplied file lines. "
        "A step depending on an undecided architecture rule must request changes; "
        "do not treat pending as approval.\n\nTask:\n" + case["task"],
    ]
    seen: dict[bytes, int] = {}
    for path, content in documents.items():
        relative = Path(path).relative_to(project).as_posix()
        row = {"path": relative, **identity(content), "delivered": content not in seen}
        normative.append(row)
        if content not in seen:
            seen[content] = len(seen)
            semantic_parts.append(f"\n## Full normative document: {relative}\n" + content.decode("utf-8"))
    for path, content in case["files"].items():
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(content.splitlines(), 1))
        semantic_parts.append(f"\n## Changed file: {path}\n{numbered}")
    semantic = "\n\n".join(semantic_parts).encode("utf-8")
    (output / "semantic.prompt.txt").write_bytes(semantic)
    for index, (path, content) in enumerate(documents.items()):
        (output / f"norm-{index:03d}.txt").write_bytes(content)
        normative[index]["captured_body"] = f"norm-{index:03d}.txt"
    result = {"workflow_digest": definition.digest, "render_seconds": time.perf_counter() - started,
              "required_skills": [s.name for s in resolution.required], "routes": routes,
              "envelopes": envelopes, "documents": normative,
              "semantic": {**identity(semantic), "raw_input_bytes": len(semantic),
                           "delivered_normative_bytes": sum(map(len, seen)), "duplicated_bytes": 0,
                           "suppressed_exact_duplicate_bytes": sum(len(b) for b in documents.values()) - sum(map(len, seen)),
                           "scope": "complete-harness-supplied-single-user-prompt; hidden-host-context-unavailable"}}
    (output / "render.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def main() -> int:
    """Parse standalone renderer arguments and render one fixture case."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("project", type=Path)
    parser.add_argument("case", type=Path)
    parser.add_argument("provider", choices=("claude", "codex"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    os.environ["HOME"] = str(args.output / "empty-render-home")
    os.environ["AGENT_FLOW_EXTERNAL_SKILLS"] = "0"
    render(args.source, args.project, json.loads(args.case.read_text(encoding="utf-8")), args.provider, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
