from typing import Protocol


class CartRepository(Protocol):
    def get_total_cents(self) -> int: ...


class DiscountRepository(Protocol):
    def get_active_percent(self) -> int: ...
