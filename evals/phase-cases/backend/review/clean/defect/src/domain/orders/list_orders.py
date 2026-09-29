from collections.abc import Iterable
from typing import Optional

from data.orders.in_memory_order_repository import InMemoryOrderRepository
from data.orders.order_row_mapper import OrderRow
from domain.orders.order import Order, OrderStatus


class ListOrders:
    """Lists one customer's orders, newest first, optionally narrowed to one status."""

    def __init__(self, rows: Iterable[OrderRow]) -> None:
        self._repository = InMemoryOrderRepository(rows)

    def __call__(self, customer_id: str, status: Optional[OrderStatus] = None) -> list[Order]:
        orders = self._repository.orders_for_customer(customer_id)
        if status is not None:
            orders = [order for order in orders if order.status is status]
        return sorted(orders, key=lambda order: order.placed_at)
