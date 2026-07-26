from __future__ import annotations

import pytest

from shop.app.dependencies import Dependencies
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


@pytest.mark.parametrize(
    "total_cents,discount_percent,expected",
    [
        (12_500, 20, 10_000),
        (12_500, 0, 12_500),
        (12_500, 100, 0),
        (12_500, 150, 0),
        (999, 15, 849),
    ],
)
def test_checkout_total(
    total_cents: int,
    discount_percent: int,
    expected: int,
) -> None:
    dependencies = Dependencies.create(total_cents, discount_percent)

    assert isinstance(dependencies.get_checkout_total, GetCheckoutTotalUseCase)
    assert dependencies.get_checkout_total() == expected
