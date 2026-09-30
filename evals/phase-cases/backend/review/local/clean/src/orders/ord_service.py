from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional, Union

ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")
STORED_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

OrderRow = Mapping[str, Union[str, int]]
OrderPayload = dict[str, str]
LoadRows = Callable[[], Iterable[OrderRow]]


def _placed_at(row: OrderRow) -> datetime:
    return datetime.strptime(str(row["placed_at"]), STORED_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _format_total(total_cents: int) -> str:
    return format(Decimal(total_cents) / 100, ".2f")


def _order_payload(row: OrderRow) -> OrderPayload:
    return {
        "id": str(row["order_id"]),
        "status": str(row["status"]),
        "total": _format_total(int(row["total_cents"])),
        "placed_at": str(row["placed_at"]),
    }


def list_customer_orders(
    load_rows: LoadRows, customer_id: str, status: Optional[str] = None
) -> list[OrderPayload]:
    """One customer's orders as response dicts, newest first, optionally narrowed to one status."""
    rows = [
        row
        for row in load_rows()
        if str(row["customer_id"]) == customer_id and (status is None or str(row["status"]) == status)
    ]
    rows.sort(key=_placed_at, reverse=True)
    return [_order_payload(row) for row in rows]
