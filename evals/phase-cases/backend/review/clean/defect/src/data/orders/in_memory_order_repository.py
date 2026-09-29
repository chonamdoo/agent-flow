from collections.abc import Iterable

from data.orders.order_row_mapper import OrderRow, order_from_row
from domain.orders.order import Order


class InMemoryOrderRepository:
    """Order storage over order rows held in memory."""

    def __init__(self, rows: Iterable[OrderRow]) -> None:
        self._rows = list(rows)

    def orders_for_customer(self, customer_id: str) -> list[Order]:
        return [order_from_row(row) for row in self._rows if row["customer_id"] == customer_id]
