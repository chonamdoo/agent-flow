from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from agent_flow.core.markers import completion_gate_marker_values


CODE_REVIEW_PHASES = frozenset(
    {
        "implement",
        "implement-fix",
        "red",
        "green",
        "refactor",
        "fix-loop",
        "final-review",
        "review",
        "pr-comment-fix",
        "pr-ci-fix",
        "multi-review",
        "architecture-review",
    }
)

APPLIED_MARKER = "project-local-skill-docs: applied"
LOCAL_SKILL_PLAN_HASH_VERSION = 1

_INCLUDE_TERMS = (
    "code development",
    "code generation",
    "code review",
    "development or review",
    "developing or reviewing",
    "implementing or reviewing",
    "writing or reviewing",
    "modifying or reviewing",
    "architecture review",
    "android code",
    "kotlin implementation",
    "compose implementation",
    "코드 개발",
    "코드 작성",
    "코드 수정",
    "코드 리뷰",
    "코드리뷰",
    "구현·리뷰",
    "개발/수정/리뷰",
    "작성·리뷰",
)

_EXCLUDE_TERMS = (
    "figma",
    "screen-spec",
    "screen spec",
    "design link",
    "figma.com/design",
    "git commit",
    "git push",
    "pull request",
    "pull-request",
    "pr-review",
    "pr review",
    "branch-pr",
    "branch base",
    "branch creation",
    "branch review",
    "release branch",
    "worktree",
    "cleanup",
    "merge cleanup",
    "merge review",
    "release-first",
    "pretooluse",
    "posttooluse",
    "guard-worktree",
    "guard-protected-branch",
    "comment-checker",
    "claude hook",
    "codex hook",
)

_EXCLUDE_TOKEN_RE = re.compile(r"(?<![a-z0-9])(pr|branch|merge)(?![a-z0-9])")


@dataclass(frozen=True)
class LocalSkillDoc:
    name: str
    path: str
    description: str = ""


def applicable_code_review_skill_docs(project_root: Path, phase_id: str) -> tuple[LocalSkillDoc, ...]:
    if phase_id not in CODE_REVIEW_PHASES:
        return ()
    return tuple(
        doc
        for doc in _project_local_skill_docs(project_root)
        if _is_code_review_skill(doc)
    )


def local_skill_prompt_block(project_root: Path, phase_id: str) -> str:
    docs = applicable_code_review_skill_docs(project_root, phase_id)
    if not docs:
        return ""
    lines = [
        "\n## Project-local code/review skills",
        "",
        "Project-local markdown skill docs that apply to code generation or code review were found.",
        "Read only the applicable docs before completing this phase. Design/Figma, hook, branch, PR, merge, and cleanup skills are intentionally excluded here.",
        "",
        "Applicable docs:",
        "",
    ]
    lines.extend(_doc_prompt_line(project_root, doc) for doc in docs)
    lines.extend(
        [
            "",
            "When this block appears, the `## Completion Gate` must include:",
            "",
            "```text",
            "project-local-skills: checked",
            "project-local-skills-used: <comma-separated applicable skill names>",
            APPLIED_MARKER,
            "```",
            "",
            "If this block is absent, `project-local-skills: n/a` remains valid.",
        ]
    )
    return "\n".join(lines) + "\n"


def _doc_prompt_line(project_root: Path, doc: LocalSkillDoc) -> str:
    doc_path = Path(doc.path)
    absolute_path = doc_path if doc_path.is_absolute() else project_root / doc_path
    return f"- `{doc.path}` (`{doc.name}`) — `{absolute_path}`"


def missing_local_skill_markers(text: str, project_root: Path, phase_id: str) -> list[str]:
    docs = applicable_code_review_skill_docs(project_root, phase_id)
    if not docs:
        return []
    values = completion_gate_marker_values(text)
    missing: list[str] = []
    if values.get("project-local-skills") != "checked":
        missing.append("project-local-skills: checked")
    used = values.get("project-local-skills-used", "").strip()
    if used in {"", "n/a", "none", "optional"}:
        missing.append("project-local-skills-used: <applicable local skill list>")
    elif not _mentions_all_docs(used, docs):
        missing.append("project-local-skills-used: <applicable local skill list>")
    if values.get("project-local-skill-docs") != "applied":
        missing.append(APPLIED_MARKER)
    return missing


def _project_local_skill_docs(project_root: Path) -> tuple[LocalSkillDoc, ...]:
    index_docs = _docs_from_index(project_root)
    if index_docs:
        return index_docs
    return _docs_from_local_skill_tree(project_root)


def project_local_skill_plan_hash(project_root: Path) -> str:
    rows: list[list[str]] = []
    for doc in _project_local_skill_docs(project_root):
        skill_path = Path(doc.path)
        absolute = skill_path if skill_path.is_absolute() else project_root / skill_path
        try:
            content_hash = hashlib.sha256(absolute.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f"blocked: unreadable project-local skill: {absolute}") from exc
        rows.append([doc.name, doc.path.replace("\\", "/"), content_hash])
    encoded = json.dumps(
        {"version": LOCAL_SKILL_PLAN_HASH_VERSION, "skills": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _docs_from_index(project_root: Path) -> tuple[LocalSkillDoc, ...]:
    index_path = project_root / ".agent-flow" / "skills" / "index.json"
    if not index_path.exists():
        return ()
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    skills = payload.get("skills")
    if not isinstance(skills, list):
        return ()
    docs: list[LocalSkillDoc] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        if item.get("source") not in {"local", "project"}:
            continue
        rel_path = str(item.get("path") or "")
        if not _is_project_local_skill_path(rel_path):
            continue
        name = str(item.get("name") or Path(rel_path).parent.name)
        description = " ".join(
            _metadata_value_text(item.get(key))
            for key in ("description", "trigger", "tags", "workflowPhases", "reviewAngles")
        )
        docs.append(LocalSkillDoc(name=name, path=rel_path, description=description))
    return _dedupe_docs(docs)


def _metadata_value_text(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def _docs_from_local_skill_tree(project_root: Path) -> tuple[LocalSkillDoc, ...]:
    docs: list[LocalSkillDoc] = []
    for base in (project_root / ".agent-flow" / "local-skills",):
        if not base.exists():
            continue
        for skill_path in sorted(base.glob("*/SKILL.md")):
            rel_path = _relative_posix(project_root, skill_path)
            docs.append(
                LocalSkillDoc(
                    name=skill_path.parent.name,
                    path=rel_path,
                    description=_metadata_text(skill_path),
                )
            )
    return _dedupe_docs(docs)


def _metadata_text(skill_path: Path) -> str:
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    frontmatter = re.match(r"\A---\n(?P<body>[\s\S]*?)\n---", text)
    if not frontmatter:
        first_lines = "\n".join(text.splitlines()[:20])
        return first_lines
    return frontmatter.group("body") + "\n" + "\n".join(text.splitlines()[0:40])


def _is_project_local_skill_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    return (
        normalized.startswith(".agent-flow/local-skills/")
        or normalized.startswith("skills/")
    ) and normalized.endswith("/SKILL.md")


def _is_code_review_skill(doc: LocalSkillDoc) -> bool:
    haystack = f"{doc.name} {doc.path} {doc.description}".lower()
    if any(term in haystack for term in _EXCLUDE_TERMS) or _EXCLUDE_TOKEN_RE.search(
        haystack
    ):
        return False
    return any(term in haystack for term in _INCLUDE_TERMS)


def _mentions_all_docs(value: str, docs: tuple[LocalSkillDoc, ...]) -> bool:
    names = {
        part.strip().strip("`").lower()
        for part in value.split(",")
        if part.strip()
    }
    return all(doc.name.lower() in names for doc in docs)


def _dedupe_docs(docs: list[LocalSkillDoc]) -> tuple[LocalSkillDoc, ...]:
    deduped: dict[str, LocalSkillDoc] = {}
    for doc in docs:
        deduped.setdefault(doc.name, doc)
    return tuple(sorted(deduped.values(), key=lambda item: item.name))


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
