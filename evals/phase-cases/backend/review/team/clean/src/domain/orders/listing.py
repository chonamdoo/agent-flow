from collections.abc import Iterable
from typing import Optional

from domain.orders.models import Order, OrderRowDTO


def list_customer_orders(
    rows: Iterable[OrderRowDTO], customer_id: str, status: Optional[str] = None
) -> list[Order]:
    """One customer's orders, newest first, optionally narrowed to one status."""
    orders = [
        Order.from_row(row)
        for row in rows
        if row.customer_id == customer_id and (status is None or row.status == status)
    ]
    return sorted(orders, key=lambda order: order.placed_at, reverse=True)
