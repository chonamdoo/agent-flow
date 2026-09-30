from collections.abc import Iterable, Mapping

from .ord_http import Handler, Response, handle_list_orders
from .repository import CallableOrderRepository, OrderRow


def build_list_orders_handler(rows: Iterable[OrderRow]) -> Handler:
    snapshot: list[OrderRow] = list(rows)
    repository = CallableOrderRepository(lambda: snapshot)

    def handler(query: Mapping[str, str]) -> Response:
        return handle_list_orders(query, repository)

    return handler


__all__ = ["build_list_orders_handler", "handle_list_orders"]
