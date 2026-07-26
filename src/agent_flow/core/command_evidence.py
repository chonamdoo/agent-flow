"""실행 관측 증거 — `record-command-run.py`가 남긴 기록을 읽는다.

`local_skills.read_skill_evidence`와 같은 모양이다. 주장자가 쓰는 마커와 달리
이 증거는 host tool 런타임이 만든다. 그래서 "안 돌렸다"는 주장자가 뒤집을 수
없다.

**이 증거가 증명하지 않는 것**을 먼저 적는다. hook은 argv와 exit code만 본다.
`pytest tests/test_x.py::test_trivial`도 exit 0이고, `assert False` 한 줄도
빨간 테스트다. 즉 관측이 확실히 잡는 것은 **"아예 안 돌렸다"** 하나뿐이며,
가짜 테스트는 관측으로 갈 수 없다. 이 층에 그 이상을 기대하면 안 된다.

hook이 없는 host에서는 로그 파일 자체가 없다. 그때는 `available=False`로
축퇴시키고 자기신고(`unavailable`)를 받는다 — L2와 같은 계약이다. 관측 불가를
위반으로 들면 hook 미지원 host에서 모든 런이 막힌다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

COMMANDS_RUN_LOG = Path(".agent-flow") / "commands-run.jsonl"

@dataclass(frozen=True)
class CommandRun:
    command: str
    exit_code: int | None
    at: float


@dataclass(frozen=True)
class CommandRunEvidence:
    available: bool
    runs: tuple[CommandRun, ...]

    def matching(self, *needles: str) -> tuple[CommandRun, ...]:
        patterns = [_needle_pattern(needle) for needle in needles if needle.strip()]
        if not patterns:
            return ()
        return tuple(
            run for run in self.runs if any(pattern.search(run.command) for pattern in patterns)
        )

    def ran(self, *needles: str) -> bool:
        return bool(self.matching(*needles))

    def failed(self, *needles: str) -> bool:
        """관측된 실패가 하나라도 있는가. exit code를 안 실어 보내는 host면 False다."""
        return any(run.exit_code not in (None, 0) for run in self.matching(*needles))

def read_command_evidence(project_root: Path, *, since: float | None = None) -> CommandRunEvidence:
    """관측 로그를 읽는다. 파일이 없으면 hook 미등록/미지원 host로 본다."""
    log_path = project_root / COMMANDS_RUN_LOG
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        return CommandRunEvidence(available=False, runs=())
    runs: list[CommandRun] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        at = entry.get("at")
        stamp = float(at) if isinstance(at, (int, float)) else 0.0
        if since is not None and stamp < since:
            continue
        code = entry.get("exit_code")
        runs.append(
            CommandRun(
                command=command,
                exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                at=stamp,
            )
        )
    return CommandRunEvidence(available=True, runs=tuple(runs))


def _needle_pattern(needle: str) -> re.Pattern[str]:
    """토큰 경계로 맞춘다. 부분 문자열로 맞추면 `mypytest`가 `pytest`로 통과한다."""
    return re.compile(rf"(?<![\w.-]){re.escape(needle.strip())}(?![\w.-])")
