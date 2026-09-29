from datetime import datetime, timezone
from typing import NamedTuple

ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")
STORED_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class OrderRowDTO(NamedTuple):
    """One stored order row as the order store hands it over."""

    order_id: str
    customer_id: str
    status: str
    total_cents: int
    placed_at: str


class Order(NamedTuple):
    id: str
    customer_id: str
    status: str
    total_cents: int
    placed_at: datetime

    @classmethod
    def from_row(cls, row: OrderRowDTO) -> "Order":
        placed_at = datetime.strptime(row.placed_at, STORED_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        return cls(row.order_id, row.customer_id, row.status, row.total_cents, placed_at)
