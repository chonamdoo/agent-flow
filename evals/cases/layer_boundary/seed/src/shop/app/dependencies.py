from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase


@dataclass(frozen=True)
class Dependencies:
    get_cart_total: GetCartTotalUseCase
    get_active_discount: GetActiveDiscountUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        return cls(
            get_cart_total=GetCartTotalUseCase(InMemoryCartRepository(total_cents)),
            get_active_discount=GetActiveDiscountUseCase(InMemoryDiscountRepository(discount_percent)),
        )
