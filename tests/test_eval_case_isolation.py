"""Shipped prompts must not learn the evaluation cases.

Once skills are tuned against `evals/phase-cases`, the cheapest way to raise a score is to
copy a case's own identifiers (its use case, its buggy function, its view-state type) into
a skill or review angle. That passes the eval and teaches nothing. This guard fails when an
identifier from a case fixture appears in a shipped prompt asset, unless it is ordinary
framework or workflow vocabulary listed below.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "evals" / "phase-cases"
SHIPPED_PROMPT_ROOTS = (
    "skills",
    "templates",
    "bootstrap",
    ".Codex/agents",
    ".claude/agents",
    "src/agent_flow/workflows",
    "src/agent_flow/profiles",
)
CASE_SUFFIXES = {".ts", ".tsx", ".js", ".cjs", ".py", ".dart", ".json", ".md"}
PROMPT_SUFFIXES = {".md", ".yaml", ".yml"}
# camelCase, PascalCase with two or more humps, or snake_case with three or more parts.
IDENTIFIER = re.compile(
    r"\b(?:[a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[a-z]+(?:_[a-z0-9]+){2,})\b"
)

# Framework, platform and workflow names that the cases use because the stack does, not
# because the case invented them. Add a name here only when it means the same thing
# outside the cases; a case's own types and functions never belong here.
GENERIC_VOCABULARY = frozenset({
    "AppShell", "ApiClient", "DataSource", "FlatList", "HttpClient", "ReactNode",
    "RepositoryImpl", "StyleSheet", "TimeoutExpired", "TypeScript", "UiModel", "UiState",
    "UseCase", "ViewModel", "accessibilityLabel", "contentType", "hydrateRoot", "keyExtractor",
    "noEmit", "onError", "pathGlobs", "renderItem", "setState", "taskTerms", "useEffect",
    "useState", "useSyncExternalStore", "workflowPhases",
})


def _identifiers(root: Path, suffixes: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in suffixes:
            for name in IDENTIFIER.findall(path.read_text(encoding="utf-8", errors="ignore")):
                found.setdefault(name, path)
    return found


def test_no_case_identifier_leaks_into_a_shipped_prompt():
    case_names = _identifiers(CASES, CASE_SUFFIXES)
    assert case_names, "phase-cases yielded no identifiers; the guard would pass vacuously"
    leaks = []
    for root in SHIPPED_PROMPT_ROOTS:
        for name, path in _identifiers(REPO / root, PROMPT_SUFFIXES).items():
            if name in case_names and name not in GENERIC_VOCABULARY:
                leaks.append(f"{name}: {path.relative_to(REPO)} (case: {case_names[name].relative_to(REPO)})")
    assert not leaks, "case identifiers in shipped prompts:\n" + "\n".join(sorted(leaks))


def test_the_guard_recognises_a_case_identifier():
    case_names = _identifiers(CASES, CASE_SUFFIXES)
    # The web case's loader is the kind of name a tuned skill would copy.
    assert "loadOrdersViewState" in case_names
    assert "loadOrdersViewState" not in GENERIC_VOCABULARY
