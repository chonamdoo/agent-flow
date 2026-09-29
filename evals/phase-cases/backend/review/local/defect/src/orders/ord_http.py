from collections.abc import Mapping
from typing import Callable

from .ord_service import ORDER_STATUSES, list_customer_orders
from .repository import OrderRepository

EMPTY_ORDERS_MESSAGE = "Nothing to show - no orders match these filters."

Response = dict[str, object]
Handler = Callable[[Mapping[str, str]], Response]


def _bad_request(error: str) -> Response:
    return {"status": 400, "body": {"error": error}}


def handle_list_orders(query: Mapping[str, str], repository: OrderRepository) -> Response:
    customer_id = query.get("customer_id")
    if not customer_id:
        return _bad_request("missing_customer_id")
    status = query.get("status")
    if status is not None and status not in ORDER_STATUSES:
        return _bad_request("invalid_status")
    orders = list_customer_orders(repository, customer_id, status)
    body: dict[str, object] = {"orders": orders}
    if not orders:
        body["message"] = EMPTY_ORDERS_MESSAGE
    return {"status": 200, "body": body}
