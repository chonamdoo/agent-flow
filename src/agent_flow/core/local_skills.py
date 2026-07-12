from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_flow.core.markers import completion_gate_marker_values
from agent_flow.core.phase_workflow import find_kit_root
from agent_flow.core.security import validate_portable_skill_name
from agent_flow.core.skill_plan import (
    hash_skill_tree,
    indexed_external_exposure_skill_names,
)


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
LOCAL_SKILL_PLAN_HASH_VERSION = 2

@dataclass(frozen=True)
class LocalSkillDoc:
    name: str
    path: str
    description: str = ""
    source: str = "project"
    tree_hash: str | None = None
    activation: str = "on-demand"
    workflow_phases: tuple[str, ...] = ()
    task_terms: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()


def applicable_project_local_skill_docs(
    project_root: Path,
    phase_id: str,
    task_scope: str | None = None,
    changed_files: Iterable[str] = (),
) -> tuple[LocalSkillDoc, ...]:
    return tuple(
        doc
        for doc in _project_local_skill_docs(project_root)
        if _skill_applies(doc, phase_id, task_scope or "", changed_files)
    )


def applicable_code_review_skill_docs(
    project_root: Path,
    phase_id: str,
    task_scope: str | None = None,
    changed_files: Iterable[str] = (),
) -> tuple[LocalSkillDoc, ...]:
    """Compatibility alias for callers that predate non-code skill routing."""
    return applicable_project_local_skill_docs(
        project_root,
        phase_id,
        task_scope,
        changed_files,
    )


def local_skill_prompt_block(
    project_root: Path,
    phase_id: str,
    task_scope: str | None = None,
    changed_files: Iterable[str] = (),
) -> str:
    docs = applicable_project_local_skill_docs(
        project_root,
        phase_id,
        task_scope,
        changed_files,
    )
    if not docs:
        return ""
    lines = [
        "\n## Project-local skills",
        "",
        "All listed deterministically applicable project-local markdown skill docs are mandatory policy for this phase.",
        "Read every listed project-local skill before completing the phase; follow references progressively when the skill points to a separate file.",
        "",
        "Applicable docs:",
        "",
    ]
    lines.extend(_doc_prompt_line(project_root, doc) for doc in docs)
    if phase_id in CODE_REVIEW_PHASES:
        lines.extend([
            "",
            "When this block appears, write these as real, unfenced Markdown lines under `## Completion Gate`:",
            "",
            "## Completion Gate",
            "project-local-skills: checked",
            "project-local-skills-used: " + ", ".join(doc.name for doc in docs),
            APPLIED_MARKER,
            "",
            "If this block is absent, `project-local-skills: n/a` remains valid.",
        ])
    else:
        lines.extend([
            "",
            "Apply these docs to this phase artifact. This non-code phase does not add local-skill completion markers.",
        ])
    return "\n".join(lines) + "\n"


def _doc_prompt_line(project_root: Path, doc: LocalSkillDoc) -> str:
    doc_path = Path(doc.path)
    absolute_path = doc_path if doc_path.is_absolute() else project_root / doc_path
    return f"- `{doc.path}` (`{doc.name}`) — `{absolute_path}`"


def missing_local_skill_markers(
    text: str,
    project_root: Path,
    phase_id: str,
    task_scope: str | None = None,
    changed_files: Iterable[str] = (),
) -> list[str]:
    if phase_id not in CODE_REVIEW_PHASES:
        return []
    docs = applicable_project_local_skill_docs(
        project_root,
        phase_id,
        task_scope,
        changed_files,
    )
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
    return _dedupe_docs(
        [*_docs_from_index(project_root), *_docs_from_local_skill_tree(project_root)]
    )


def project_local_skill_plan_hash(project_root: Path) -> str:
    payload = {
        "version": LOCAL_SKILL_PLAN_HASH_VERSION,
        "skills": [
            [
                doc.name,
                doc.path.replace("\\", "/"),
                doc.source,
                doc.tree_hash,
                doc.activation,
                sorted(doc.workflow_phases),
                sorted(doc.task_terms),
                sorted(doc.path_globs),
            ]
            for doc in _project_local_skill_docs(project_root)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _docs_from_index(project_root: Path) -> tuple[LocalSkillDoc, ...]:
    index_path = project_root / ".agent-flow" / "skills" / "index.json"
    if not index_path.exists() and not index_path.is_symlink():
        return ()
    _require_regular_directory(
        project_root,
        index_path.parent,
        "project-local skill index parent",
    )
    mode = _lstat_mode(index_path, "project-local skill index")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError(
            f"blocked: project-local skill index must be a regular file: {index_path}"
        )
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"blocked: unreadable project-local skill index: {index_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"blocked: invalid project-local skill index: {index_path}")
    skills = payload.get("skills")
    if not isinstance(skills, list):
        raise RuntimeError(f"blocked: invalid project-local skill index: {index_path}")
    selection = payload.get("selection")
    external_exposure_skills = set(
        indexed_external_exposure_skill_names(selection)
    )

    docs: list[LocalSkillDoc] = []
    for item in skills:
        if not isinstance(item, dict):
            raise RuntimeError(f"blocked: invalid project-local skill index: {index_path}")
        discovery_source = item.get("discovery_source") or item.get("source")
        provenance_source = item.get("source")
        raw_name = item.get("name")
        exposed_external = False
        if isinstance(raw_name, str):
            try:
                logical_name = validate_portable_skill_name(raw_name).lower()
            except ValueError:
                logical_name = ""
            exposed_external = (
                logical_name in external_exposure_skills
                and provenance_source in {"host-bootstrap", "shared"}
            )
        if (
            discovery_source not in {"local", "project"}
            and not exposed_external
        ):
            continue
        rel_path = str(item.get("path") or "")
        if not _is_project_local_skill_path(rel_path):
            raise RuntimeError(f"blocked: invalid project-local skill path in index: {rel_path!r}")
        skill_path = _safe_skill_path(project_root, rel_path)
        expected_tree_hash = item.get("tree_hash")
        if expected_tree_hash is not None:
            if not isinstance(expected_tree_hash, str) or not expected_tree_hash:
                raise RuntimeError(
                    f"blocked: invalid project-local skill tree hash in index: {rel_path!r}"
                )
            if hash_skill_tree(skill_path.parent) != expected_tree_hash:
                raise RuntimeError(
                    f"blocked: project-local skill tree hash mismatch: {rel_path!r}"
                )
        raw_name = item.get("name") if "name" in item else Path(rel_path).parent.name
        try:
            name = validate_portable_skill_name(raw_name).lower()  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"blocked: invalid project-local skill name in index: {raw_name!r}"
            ) from exc
        activation = _activation(item, name)
        workflow_phases = _routing_strings(item, "workflowPhases", name)
        task_terms = _routing_strings(item, "taskTerms", name)
        path_globs = _routing_strings(item, "pathGlobs", name)
        for term in task_terms:
            if not _normalize_task_text(term):
                raise RuntimeError(
                    f"blocked: invalid project-local skill taskTerms for {name}"
                )
        for pattern in path_globs:
            _validate_path_glob(pattern, name)
        if activation == "conditional" and not task_terms and not path_globs:
            raise RuntimeError(
                f"blocked: conditional project-local skill has no selectors: {name}"
            )
        description = " ".join(
            _metadata_value_text(item.get(key))
            for key in ("description", "trigger", "tags", "workflowPhases", "reviewAngles")
        )
        docs.append(
            LocalSkillDoc(
                name=name,
                path=_relative_posix(project_root, skill_path),
                description=description,
                source=str(
                    provenance_source
                    if exposed_external
                    else discovery_source
                ),
                tree_hash=hash_skill_tree(skill_path.parent),
                activation=activation,
                workflow_phases=workflow_phases,
                task_terms=task_terms,
                path_globs=path_globs,
            )
        )
    return _dedupe_docs(docs)


def _metadata_value_text(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def _activation(item: dict[str, object], name: str) -> str:
    value = item.get("activation")
    if value is None:
        return "on-demand"
    if not isinstance(value, str) or value not in {
        "always",
        "conditional",
        "on-demand",
    }:
        raise RuntimeError(f"blocked: invalid project-local skill activation for {name}")
    return value


def _routing_strings(
    item: dict[str, object],
    key: str,
    name: str,
) -> tuple[str, ...]:
    if key not in item:
        return ()
    value = item[key]
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise RuntimeError(
            f"blocked: invalid project-local skill {key} for {name}"
        )
    return tuple(dict.fromkeys(entry.strip() for entry in value))


def _skill_applies(
    doc: LocalSkillDoc,
    phase_id: str,
    task_scope: str,
    changed_files: Iterable[str],
) -> bool:
    if doc.activation == "always":
        if doc.workflow_phases:
            return _portable_casefold(phase_id) in {
                _portable_casefold(phase) for phase in doc.workflow_phases
            }
        return phase_id in CODE_REVIEW_PHASES
    if doc.activation == "on-demand":
        return False
    if doc.workflow_phases and _portable_casefold(phase_id) not in {
        _portable_casefold(phase) for phase in doc.workflow_phases
    }:
        return False
    normalized_task = f" {_normalize_task_text(task_scope)} "
    task_match = any(
        f" {_normalize_task_text(term)} " in normalized_task
        for term in doc.task_terms
    )
    normalized_files = tuple(_normalize_repo_path(path) for path in changed_files)
    path_match = any(
        _path_glob_matches(pattern, path)
        for pattern in doc.path_globs
        for path in normalized_files
    )
    return task_match or path_match


def _normalize_task_text(value: str) -> str:
    folded = _portable_casefold(value)
    return " ".join(
        "".join(character if character.isalnum() else " " for character in folded).split()
    )


def _normalize_repo_path(value: str) -> str:
    return _portable_casefold(value).replace("\\", "/")


def _portable_casefold(value: object) -> str:
    return (
        unicodedata.normalize("NFKC", str(value))
        .lower()
        .replace("ß", "ss")
        .replace("ς", "σ")
    )


def _validate_path_glob(pattern: str, name: str) -> None:
    normalized = pattern.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RuntimeError(f"blocked: invalid project-local skill pathGlobs for {name}")


def _path_glob_matches(pattern: str, candidate: str) -> bool:
    folded_pattern = _normalize_repo_path(pattern)
    expression: list[str] = ["^"]
    index = 0
    while index < len(folded_pattern):
        if folded_pattern.startswith("**/", index):
            expression.append("(?:.*/)?")
            index += 3
        elif folded_pattern.startswith("**", index):
            expression.append(".*")
            index += 2
        elif folded_pattern[index] == "*":
            expression.append("[^/]*")
            index += 1
        elif folded_pattern[index] == "?":
            expression.append("[^/]")
            index += 1
        else:
            expression.append(re.escape(folded_pattern[index]))
            index += 1
    expression.append("$")
    return re.fullmatch("".join(expression), candidate) is not None


def _docs_from_local_skill_tree(project_root: Path) -> tuple[LocalSkillDoc, ...]:
    docs: list[LocalSkillDoc] = []
    bases = [(project_root / ".agent-flow" / "local-skills", "local")]
    if project_root.resolve() != find_kit_root().resolve():
        bases.append((project_root / "skills", "project"))
    for base, source in bases:
        if not base.exists() and not base.is_symlink():
            continue
        _require_regular_directory(project_root, base, "project-local skill root")
        for skill_dir in sorted(base.iterdir(), key=lambda path: path.name):
            mode = _lstat_mode(skill_dir, "project-local skill")
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"blocked: project-local skill may not be a symlink: {skill_dir}")
            if not stat.S_ISDIR(mode):
                continue
            skill_path = skill_dir / "SKILL.md"
            if not skill_path.exists() and not skill_path.is_symlink():
                continue
            _validate_skill_tree(project_root, skill_dir)
            try:
                skill_text = skill_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"blocked: project-local skill is unreadable: {skill_path}"
                ) from exc
            line_count = len(re.split(r"\r?\n", skill_text))
            if line_count > 200:
                raise RuntimeError(
                    f"blocked: {skill_path}: {line_count} lines; max is 200, "
                    "split progressive references"
                )
            rel_path = _relative_posix(project_root, skill_path)
            (
                name,
                activation,
                workflow_phases,
                task_terms,
                path_globs,
            ) = _tree_skill_routing(skill_text, skill_path.parent.name)
            docs.append(
                LocalSkillDoc(
                    name=name,
                    path=rel_path,
                    description=_metadata_text(skill_path),
                    source=source,
                    tree_hash=hash_skill_tree(skill_dir),
                    activation=activation,
                    workflow_phases=workflow_phases,
                    task_terms=task_terms,
                    path_globs=path_globs,
                )
            )
    return _dedupe_docs(docs)


def _tree_skill_name(text: str, fallback_name: str) -> str:
    metadata = parse_skill_frontmatter(text)
    raw_name = metadata.get("name") if "name" in metadata else fallback_name
    if not isinstance(raw_name, str):
        raise RuntimeError(f"blocked: unsafe project-local skill name: {raw_name!r}")
    try:
        return validate_portable_skill_name(raw_name).lower()
    except ValueError as exc:
        raise RuntimeError(f"blocked: unsafe project-local skill name: {raw_name}") from exc


def _tree_skill_routing(
    text: str,
    fallback_name: str,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    metadata = parse_skill_frontmatter(text)
    name = _tree_skill_name(text, fallback_name)
    activation = _activation(metadata, name)
    workflow_phases = _routing_strings(metadata, "workflowPhases", name)
    task_terms = _routing_strings(metadata, "taskTerms", name)
    path_globs = _routing_strings(metadata, "pathGlobs", name)
    for term in task_terms:
        if not _normalize_task_text(term):
            raise RuntimeError(
                f"blocked: invalid project-local skill taskTerms for {name}"
            )
    for pattern in path_globs:
        _validate_path_glob(pattern, name)
    if activation == "conditional" and not task_terms and not path_globs:
        raise RuntimeError(
            f"blocked: conditional project-local skill has no selectors: {name}"
        )
    return name, activation, workflow_phases, task_terms, path_globs


def parse_skill_frontmatter(text: str) -> dict[str, object]:
    frontmatter = re.match(
        r"\A---\r?\n(?P<body>[\s\S]*?)\r?\n---(?:\r?\n|\Z)",
        text,
    )
    if not frontmatter:
        return {}
    metadata: dict[str, object] = {}
    list_key: str | None = None
    for line in frontmatter.group("body").splitlines():
        list_item = re.match(r"^\s+-\s*(.+)$", line)
        if list_item and list_key:
            value = metadata[list_key]
            if isinstance(value, list):
                value.append(_unquote_frontmatter(list_item.group(1).strip()))
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            list_key = None
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            metadata[key] = [
                _unquote_frontmatter(item.strip())
                for item in raw[1:-1].split(",")
                if item.strip()
            ]
            list_key = None
        elif raw == "":
            metadata[key] = []
            list_key = key
        else:
            metadata[key] = _unquote_frontmatter(raw)
            list_key = None
    return metadata


def _unquote_frontmatter(value: str) -> str:
    return re.sub(r"^['\"]|['\"]$", "", value)


def _metadata_text(skill_path: Path) -> str:
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    frontmatter = re.match(r"\A---\r?\n(?P<body>[\s\S]*?)\r?\n---", text)
    if not frontmatter:
        first_lines = "\n".join(text.splitlines()[:20])
        return first_lines
    body = frontmatter.group("body").replace("\r\n", "\n")
    return body + "\n" + "\n".join(text.splitlines()[0:40])


def _is_project_local_skill_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or "//" in normalized:
        return False
    parts = tuple(normalized.split("/"))
    return (
        len(parts) == 4
        and parts[:2] in {
            (".agent-flow", "local-skills"),
            (".agent-flow", "skills"),
        }
        and parts[-1] == "SKILL.md"
        and parts[2] not in {".", ".."}
    ) or (
        len(parts) == 3
        and parts[0] == "skills"
        and parts[-1] == "SKILL.md"
        and parts[1] not in {".", ".."}
    )


def _mentions_all_docs(value: str, docs: tuple[LocalSkillDoc, ...]) -> bool:
    names = {
        part.strip().strip("`").lower()
        for part in value.split(",")
        if part.strip()
    }
    return all(doc.name.lower() in names for doc in docs)


def _dedupe_docs(docs: list[LocalSkillDoc]) -> tuple[LocalSkillDoc, ...]:
    deduped: dict[str, LocalSkillDoc] = {}
    priority = {"local": 0, "project": 1}
    for doc in docs:
        try:
            key = validate_portable_skill_name(doc.name).lower()
        except ValueError as exc:
            raise RuntimeError(
                f"blocked: invalid project-local skill name: {doc.name!r}"
            ) from exc
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = doc
            continue
        existing_path = existing.path.replace("\\", "/")
        next_path = doc.path.replace("\\", "/")
        if (
            existing_path == next_path
            and existing.tree_hash
            and doc.tree_hash
            and existing.tree_hash != doc.tree_hash
        ):
            raise RuntimeError(
                f"blocked: conflicting project-local skill snapshot: {doc.name}"
            )
        if existing_path == next_path:
            continue
        existing_priority = priority.get(existing.source, 99)
        next_priority = priority.get(doc.source, 99)
        if next_priority < existing_priority:
            deduped[key] = doc
        elif next_priority == existing_priority:
            raise RuntimeError(
                f"blocked: conflicting project-local skill paths: {doc.name}"
            )
    return tuple(sorted(deduped.values(), key=lambda item: item.name))


def _safe_skill_path(project_root: Path, rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/")
    skill_path = project_root / Path(normalized)
    parts = tuple(normalized.split("/"))
    if parts[:2] == (".agent-flow", "local-skills"):
        base = project_root / ".agent-flow" / "local-skills"
    elif parts[:2] == (".agent-flow", "skills"):
        base = project_root / ".agent-flow" / "skills"
    elif parts[:1] == ("skills",):
        base = project_root / "skills"
    else:
        raise RuntimeError(f"blocked: invalid project-local skill path: {rel_path!r}")
    _require_regular_directory(project_root, base, "project-local skill root")
    _validate_skill_tree(project_root, skill_path.parent)
    mode = _lstat_mode(skill_path, "project-local SKILL.md")
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"blocked: project-local SKILL.md is not a regular file: {skill_path}")
    return skill_path


def _validate_skill_tree(project_root: Path, skill_dir: Path) -> None:
    _require_regular_directory(project_root, skill_dir, "project-local skill")
    pending = [skill_dir]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise RuntimeError(f"blocked: project-local skill is unreadable: {directory}") from exc
        for entry in entries:
            mode = _lstat_mode(entry, "project-local skill entry")
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"blocked: project-local skill may not contain symlinks: {entry}")
            if stat.S_ISDIR(mode):
                pending.append(entry)
            elif not stat.S_ISREG(mode):
                raise RuntimeError(f"blocked: unsupported project-local skill entry: {entry}")


def _require_regular_directory(project_root: Path, path: Path, label: str) -> None:
    lexical_root = Path(os.path.abspath(project_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise RuntimeError(f"blocked: {label} escapes the project: {path}") from exc
    cursor = lexical_root
    for part in relative.parts:
        cursor /= part
        mode = _lstat_mode(cursor, label)
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"blocked: {label} must be a real directory: {cursor}")
    try:
        lexical_path.resolve(strict=True).relative_to(lexical_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"blocked: {label} escapes the project: {path}") from exc


def _lstat_mode(path: Path, label: str) -> int:
    try:
        return path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(f"blocked: {label} is unreadable: {path}") from exc


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
