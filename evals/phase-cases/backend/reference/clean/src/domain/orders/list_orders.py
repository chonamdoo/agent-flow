from typing import Optional

from domain.orders.order import Order, OrderStatus
from domain.orders.order_repository import OrderRepository


class ListOrders:
    """Lists one customer's orders, newest first, optionally narrowed to one status."""

    def __init__(self, repository: OrderRepository) -> None:
        self._repository = repository

    def __call__(self, customer_id: str, status: Optional[OrderStatus] = None) -> list[Order]:
        orders = self._repository.orders_for_customer(customer_id)
        if status is not None:
            orders = [order for order in orders if order.status is status]
        return sorted(orders, key=lambda order: order.placed_at, reverse=True)
