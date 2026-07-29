"""SPEC-7: Node 판정 코드 삭제 후 죽은 심볼이 kit.mjs에 없음을 검증."""
from __future__ import annotations

from pathlib import Path

import pytest

KIT = (Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")

DEAD_SYMBOLS = [
    "function assertCompletionMarkers(",
    "function nextPhaseIndex(",
    "function syncRouteArtifacts(",
    "function missingMarkers(",
    "function runSpecCli(",
    "function captureSpecLedger(",
    "function missingMarkersForPhase(",
    "function missingProjectLocalSkillMarkers(",
    "function missingSpecMarkers(",
    "function writeTempArtifact(",
    "function artifactHasFailureMarkers(",
    "function nextFixLoopRounds(",
    "function nodeRouteKey(",
    "function readArtifactStatus(",
    "function readArtifactVerdict(",
    "function assertMinReviewerCount(",
    "function readMultiReviewVerdict(",
    "function unfencedMarkdownText(",
    "function readMultiReviewOverallVerdict(",
    "function parseReviewerVerdicts(",
    "function hasSubagentSource(",
    "function isSubagentSource(",
    "function normalizeReviewerId(",
    "function normalizeReviewerHeadingId(",
    "const FIX_LOOP_MAX_ROUNDS",
]


@pytest.mark.parametrize("symbol", DEAD_SYMBOLS)
def test_dead_symbol_absent(symbol: str):
    """불변: SPEC-7에서 삭제된 Node 판정 심볼은 kit.mjs에 없어야 한다."""
    assert symbol not in KIT, f"{symbol!r} 이 kit.mjs에 남아 있음"
