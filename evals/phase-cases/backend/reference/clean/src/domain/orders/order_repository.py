from typing import Protocol

from domain.orders.order import Order


class OrderRepository(Protocol):
    def orders_for_customer(self, customer_id: str) -> list[Order]: ...
