"""Upstream 429 재시도 백오프."""
from __future__ import annotations


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    """`attempt`번째 재시도까지 기다릴 초. 아직 구현되지 않았다."""
    raise NotImplementedError
