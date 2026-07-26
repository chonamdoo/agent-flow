from __future__ import annotations

import statistics

from backoff import next_delay


def test_grows_exponentially_from_the_base():
    assert 0.5 <= next_delay(0) <= 1.0
    assert 1.0 <= next_delay(1) <= 2.0
    assert 2.0 <= next_delay(2) <= 4.0


def test_is_capped():
    assert all(next_delay(attempt) <= 30.0 for attempt in range(20))


def test_is_jittered():
    """같은 attempt가 늘 같은 값이면 재시도가 한 틱에 몰린다."""
    samples = [next_delay(3) for _ in range(200)]
    assert len(set(samples)) > 100
    assert statistics.pstdev(samples) > 0.1
