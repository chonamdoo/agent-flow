from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PhaseArtifact:
    stage_id: str
    path: Path
    status: str
    verdict: str
    evidence_type: str
    confidence: str
    stale: bool = False


def read_phase_artifact(path: Path) -> PhaseArtifact:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    stage_id = _stage_id(path, text)
    return PhaseArtifact(
        stage_id=stage_id,
        path=path,
        status=_field(text, "status") or "unknown",
        verdict=_field(text, "verdict") or "",
        evidence_type=_field(text, "evidence type") or _field(text, "evidence_type") or "observed",
        confidence=_field(text, "confidence") or "unknown",
        stale=_field(text, "stale").lower() == "true",
    )


def _stage_id(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+Stage Result:\s*(.+)$", line.strip(), re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return path.stem


def _field(text: str, name: str) -> str:
    pattern = re.compile(
        rf"^\s*(?:[-*]\s*)?{re.escape(name)}\s*:\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""
