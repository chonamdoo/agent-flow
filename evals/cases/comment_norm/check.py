"""`comment_norm` 기계 oracle.

두 축 모두 agent가 쓰지 못하는 판정자를 쓴다 — 하나는 pytest, 하나는 이
저장소의 comment-checker hook이다. agent가 "했다"고 적은 마커는 보지 않는다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[3]
COMMENT_CHECKER = KIT_ROOT / "scripts" / "hooks" / "comment-checker.py"
TARGET = "backoff.py"


def score(project: Path) -> dict[str, bool]:
    return {"behavior": _tests_pass(project), "norm": _comments_carry_a_reason(project)}


def _tests_pass(project: Path) -> bool:
    result = subprocess.run(
        (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="),
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def _comments_carry_a_reason(project: Path) -> bool:
    """comment-checker가 저가치 주석을 하나도 못 찾으면 통과.

    seed 원문에 이미 있던 주석은 payload의 `old_string`으로 넘겨 제외한다.
    구현하며 **새로 쓴** 주석만 판정 대상이다.
    """
    target = project / TARGET
    if not target.is_file():
        return False
    seed = Path(__file__).parent / "seed" / TARGET
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": TARGET,
            "content": target.read_text(encoding="utf-8"),
            "old_string": seed.read_text(encoding="utf-8"),
        },
    }
    result = subprocess.run(
        (sys.executable, str(COMMENT_CHECKER)),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0
