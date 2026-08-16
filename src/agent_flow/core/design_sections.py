"""DDD 설계 문서가 요구된 섹션을 실제로 담고 있는가.

제목만 본다. 내용의 품질은 이 층이 증명할 수 없고, 잡으려는 것은 "그 절을 아예
쓰지 않았다" 하나다. 별칭 목록은 한국어/영어 표기를 함께 받는다 — 표기가 갈린다고
설계가 빠진 것은 아니다.
"""
from __future__ import annotations

import re
from pathlib import Path

DDD_REQUIRED_DESIGN_SECTIONS = (
    ("bounded context", ("bounded context", "bounded contexts", "context map", "컨텍스트")),
    ("ubiquitous language", ("ubiquitous language", "ubiquitous language terms", "domain language", "보편 언어", "유비쿼터스 언어")),
    ("aggregate", ("aggregate", "aggregates", "aggregate root", "애그리거트")),
    ("entity", ("entity", "entities", "엔티티")),
    ("value object", ("value object", "value objects", "vo", "값 객체")),
    ("domain event", ("domain event", "domain events", "도메인 이벤트")),
    ("domain invariant", ("domain invariant", "domain invariants", "invariant", "invariants", "도메인 불변식", "불변식")),
    ("domain flow", ("domain flow", "domain flows", "domain workflow", "domain workflows", "도메인 흐름")),
)


def missing_ddd_design_terms(run_dir: Path) -> list[str]:
    candidates = [run_dir / "ddd-design.md", run_dir / "design.md"]
    text = ""
    for candidate in candidates:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            break
    if not text:
        return ["ddd-design.md or design.md"]

    section_titles = _design_section_titles(text)
    if any(_section_title_matches(section_titles, alias) for alias in ("service-layer refactor", "service layer refactor")):
        return ["ddd mode cannot be service-layer refactor"]
    return [
        label
        for label, aliases in DDD_REQUIRED_DESIGN_SECTIONS
        if not any(_section_title_matches(section_titles, alias) for alias in aliases)
    ]


def _design_section_titles(text: str) -> list[str]:
    titles: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        # DDD 판정은 Markdown heading과 list label만 본다. 본문 문장은 relay를 막지 않는다.
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            titles.append(_normalize_design_heading(heading.group(1)))
            continue
        label = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(?:\*\*)?([^:]{1,80}?)(?:\*\*)?\s*:", stripped)
        if label:
            titles.append(_normalize_design_heading(label.group(1)))
    return titles


def _normalize_design_heading(value: str) -> str:
    lowered = value.lower()
    cleaned = re.sub(r"[`*_#]+", " ", lowered)
    cleaned = re.sub(r"^\s*\d+(?:[.)]|\s+-)\s*", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" :-")


def _section_title_matches(section_titles: list[str], alias: str) -> bool:
    normalized_alias = _normalize_design_heading(alias)
    return any(title == normalized_alias or title.startswith(f"{normalized_alias} ") for title in section_titles)
