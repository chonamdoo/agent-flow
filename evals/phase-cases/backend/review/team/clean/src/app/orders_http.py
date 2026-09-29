from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any, Callable

from app.responses import Response, bad_request, ok
from domain.orders.listing import list_customer_orders
from domain.orders.models import ORDER_STATUSES, Order, OrderRowDTO

EMPTY_ORDERS_MESSAGE = "Nothing to show - no orders match these filters."
RESPONSE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

LoadOrderRows = Callable[[], Iterable[OrderRowDTO]]


def _format_total(total_cents: int) -> str:
    return format(Decimal(total_cents) / 100, ".2f")


def _order_payload(order: Order) -> dict[str, str]:
    return {
        "id": order.id,
        "status": order.status,
        "total": _format_total(order.total_cents),
        "placed_at": order.placed_at.strftime(RESPONSE_TIMESTAMP_FORMAT),
    }


def handle_list_orders(query: Mapping[str, str], load_rows: LoadOrderRows) -> Response:
    customer_id = query.get("customer_id")
    if not customer_id:
        return bad_request("missing_customer_id")
    status = query.get("status")
    if status is not None and status not in ORDER_STATUSES:
        return bad_request("invalid_status")
    orders = [_order_payload(order) for order in list_customer_orders(load_rows(), customer_id, status)]
    body: dict[str, Any] = {"orders": orders}
    if not orders:
        body["message"] = EMPTY_ORDERS_MESSAGE
    return ok(body)
