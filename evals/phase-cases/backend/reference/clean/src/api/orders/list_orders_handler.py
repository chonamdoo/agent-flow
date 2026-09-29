from collections.abc import Mapping
from decimal import Decimal
from typing import Callable, Optional, TypedDict

from domain.orders.list_orders import ListOrders
from domain.orders.order import Order, OrderStatus

EMPTY_ORDERS_MESSAGE = "Nothing to show - no orders match these filters."
RESPONSE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Response(TypedDict):
    status: int
    body: dict[str, object]


Handler = Callable[[Mapping[str, str]], Response]


def _format_total(total_cents: int) -> str:
    return format(Decimal(total_cents) / 100, ".2f")


def _order_payload(order: Order) -> dict[str, str]:
    return {
        "id": order.id,
        "status": order.status.value,
        "total": _format_total(order.total_cents),
        "placed_at": order.placed_at.strftime(RESPONSE_TIMESTAMP_FORMAT),
    }


def _bad_request(error: str) -> Response:
    return {"status": 400, "body": {"error": error}}


def make_list_orders_handler(list_orders: ListOrders) -> Handler:
    def handle(query: Mapping[str, str]) -> Response:
        customer_id = query.get("customer_id")
        if not customer_id:
            return _bad_request("missing_customer_id")
        status: Optional[OrderStatus] = None
        raw_status = query.get("status")
        if raw_status is not None:
            try:
                status = OrderStatus(raw_status)
            except ValueError:
                return _bad_request("invalid_status")
        orders = [_order_payload(order) for order in list_orders(customer_id, status)]
        body: dict[str, object] = {"orders": orders}
        if not orders:
            body["message"] = EMPTY_ORDERS_MESSAGE
        return {"status": 200, "body": body}

    return handle
