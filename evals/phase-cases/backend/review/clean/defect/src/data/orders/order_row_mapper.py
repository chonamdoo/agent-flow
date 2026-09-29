from datetime import datetime, timezone
from typing import TypedDict

from domain.orders.order import Order, OrderStatus

STORED_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class OrderRow(TypedDict):
    """One stored order row as the order store hands it over."""

    order_id: str
    customer_id: str
    status: str
    total_cents: int
    placed_at: str


def order_from_row(row: OrderRow) -> Order:
    return Order(
        id=row["order_id"],
        customer_id=row["customer_id"],
        status=OrderStatus(row["status"]),
        total_cents=row["total_cents"],
        placed_at=datetime.strptime(row["placed_at"], STORED_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc),
    )
