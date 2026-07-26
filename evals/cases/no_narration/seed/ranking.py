"""리더보드 순위 계산."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    name: str
    score: int


def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순. 아직 구현되지 않았다."""
    raise NotImplementedError
