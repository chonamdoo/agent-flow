from __future__ import annotations

import json
import re
import sys
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


ARCHITECTURE_MODES = ("clean", "local", "pending")
GOVERNANCE_KEYS = frozenset({"version", "owner", "lifecycle", "approval", "provenance"})
_NORMATIVE_KEYS = frozenset({
    "requires", "dependencies", "requires_by_architecture", "architecture_modes", "requires_docs",
})


class SkillMetadataError(ValueError):
    pass


class InvalidSkillFrontmatter(SkillMetadataError):
    pass


def split_frontmatter(text: str, *, source: str) -> tuple[str | None, str]:
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1:])
    raise InvalidSkillFrontmatter(f"{source}: unterminated frontmatter")


def reject_duplicate_keys(node: Node, *, source: str) -> None:
    active: set[int] = set()
    visited: set[int] = set()
    pending = [(node, False)]
    while pending:
        current, leaving = pending.pop()
        identity = id(current)
        if leaving:
            active.remove(identity)
            visited.add(identity)
            continue
        if identity in active:
            raise SkillMetadataError(f"{source}: recursive YAML aliases are not supported")
        if identity in visited:
            continue
        active.add(identity)
        pending.append((current, True))
        if isinstance(current, MappingNode):
            seen: set[str] = set()
            for key, value in current.value:
                if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                    raise SkillMetadataError(
                        f"{source}: YAML mapping keys must be strings; merges are unsupported"
                    )
                if key.value in seen:
                    raise SkillMetadataError(f"{source}: duplicate key {key.value!r}")
                seen.add(key.value)
                pending.append((value, False))
        elif isinstance(current, SequenceNode):
            pending.extend((child, False) for child in current.value)


def _reject_merges(node: Node, *, source: str) -> None:
    pending = [node]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if current.tag == "tag:yaml.org,2002:merge":
            raise SkillMetadataError(f"{source}: YAML merges are unsupported in skill metadata")
        if isinstance(current, MappingNode):
            for key, value in current.value:
                pending.extend((key, value))
        elif isinstance(current, SequenceNode):
            pending.extend(current.value)


def parse_skill_metadata(
    text: str, *, source: str, strict_duplicates: bool = False,
) -> dict[str, Any] | None:
    """이미 읽은 문서의 규범 메타데이터를 검증하고 governance scalar 원문을 보존한다."""
    header, _ = split_frontmatter(text, source=source)
    if header is None:
        return None
    try:
        loader = yaml.SafeLoader(header)
    except yaml.YAMLError as exc:
        raise InvalidSkillFrontmatter(f"{source}: invalid frontmatter: {exc}") from exc
    try:
        document = loader.get_single_node()
        if not isinstance(document, MappingNode):
            raise InvalidSkillFrontmatter(f"{source}: frontmatter must be a mapping")
        _reject_merges(document, source=source)
        normative = document if strict_duplicates else MappingNode(
            document.tag,
            [
                (key, value) for key, value in document.value
                if isinstance(key, ScalarNode) and key.value in _NORMATIVE_KEYS
            ],
        )
        reject_duplicate_keys(normative, source=source)
        parsed = loader.construct_document(document)
    except (yaml.YAMLError, RecursionError) as exc:
        raise InvalidSkillFrontmatter(f"{source}: invalid frontmatter: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(parsed, dict):
        raise InvalidSkillFrontmatter(f"{source}: frontmatter must be a mapping")
    governance_nodes = {
        key.value: value for key, value in document.value
        if isinstance(key, ScalarNode) and key.value in GOVERNANCE_KEYS
    }
    for key, value in governance_nodes.items():
        if isinstance(value, ScalarNode):
            parsed[key] = value.value
    _validate_normative_metadata(parsed, source=source)
    return parsed


def is_safe_skill_name(value: object) -> bool:
    name = str(value)
    if set(name) <= {"."}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))


def _validate_skill_list(names: object, *, source: str, key: str) -> None:
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not is_safe_skill_name(name) for name in names
    ):
        raise SkillMetadataError(f"{source}: {key} must be a skill list")


def _validate_normative_metadata(metadata: dict[str, Any], *, source: str) -> None:
    for key in ("requires", "dependencies"):
        if key in metadata:
            _validate_skill_list(metadata[key], source=source, key=key)
    if "architecture_modes" in metadata:
        modes = metadata["architecture_modes"]
        if (
            not isinstance(modes, list)
            or not modes
            or any(not isinstance(mode, str) or mode not in ARCHITECTURE_MODES for mode in modes)
        ):
            raise SkillMetadataError(
                f"{source}: architecture_modes must be a nonempty architecture mode list"
            )
    if "requires_by_architecture" in metadata:
        declared = metadata["requires_by_architecture"]
        if not isinstance(declared, dict) or any(mode not in ARCHITECTURE_MODES for mode in declared):
            raise SkillMetadataError(
                f"{source}: requires_by_architecture must map architecture modes to skill lists"
            )
        for mode, names in declared.items():
            _validate_skill_list(names, source=source, key=f"requires_by_architecture.{mode}")
    if "requires_docs" in metadata:
        declared = metadata["requires_docs"]
        if not isinstance(declared, list):
            raise SkillMetadataError(f"{source}: requires_docs must be a list")
        seen: set[str] = set()
        for entry in declared:
            reference = _validate_reference(entry, source=source)
            if reference in seen:
                raise SkillMetadataError(f"{source}: duplicate requires_docs entry: {reference}")
            seen.add(reference)


def _validate_reference(entry: object, *, source: str) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise SkillMetadataError(f"{source}: requires_docs entry must be a path: {entry!r}")
    segments = entry.split("/")
    if (
        entry != entry.strip()
        or len(segments) < 2
        or segments[0] != "references"
        or any(part in {"", ".", ".."} for part in segments)
        or "\\" in entry
        or any(ord(character) < 32 for character in entry)
        or not entry.endswith(".md")
    ):
        raise SkillMetadataError(
            f"{source}: requires_docs must name canonical references/*.md paths: {entry!r}"
        )
    return entry


def parse_metadata_batch(payload: object) -> dict[str, Any]:
    """검증된 JSON 요청의 각 문서를 같은 파서로 처리하고 규범 필드만 반환한다."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "documents"}:
        raise ValueError("metadata request must contain schema_version and documents")
    version = payload["schema_version"]
    if type(version) is not int or version != 1:
        raise ValueError("metadata request schema_version must be 1")
    documents = payload["documents"]
    if not isinstance(documents, list):
        raise ValueError("metadata request documents must be a list")
    for document in documents:
        if (
            not isinstance(document, dict)
            or set(document) != {"source", "text"}
            or not isinstance(document["source"], str)
            or not isinstance(document["text"], str)
        ):
            raise ValueError("each metadata document must contain source and text strings")
    results = []
    for document in documents:
        source = document["source"]
        try:
            metadata = parse_skill_metadata(document["text"], source=source)
        except SkillMetadataError as exc:
            results.append({"source": source, "metadata": None, "error": str(exc)})
        else:
            projection = (
                {key: value for key, value in metadata.items() if key in _NORMATIVE_KEYS}
                if metadata is not None else None
            )
            results.append({"source": source, "metadata": projection, "error": None})
    return {"schema_version": 1, "documents": results}


def main() -> int:
    try:
        result = parse_metadata_batch(json.load(sys.stdin))
        json.dump(result, sys.stdout, ensure_ascii=True, allow_nan=False)
        sys.stdout.write("\n")
    except (ValueError, OSError) as exc:
        print(f"skill metadata protocol: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
