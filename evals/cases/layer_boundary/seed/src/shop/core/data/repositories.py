from dataclasses import dataclass


@dataclass(frozen=True)
class InMemoryCartRepository:
    total_cents: int

    def get_total_cents(self) -> int:
        return self.total_cents


@dataclass(frozen=True)
class InMemoryDiscountRepository:
    active_percent: int

    def get_active_percent(self) -> int:
        return self.active_percent
