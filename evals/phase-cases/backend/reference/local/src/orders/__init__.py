from collections.abc import Iterable, Mapping

from .ord_http import Handler, Response, handle_list_orders
from .ord_service import OrderRow


def build_list_orders_handler(rows: Iterable[OrderRow]) -> Handler:
    snapshot: list[OrderRow] = list(rows)

    def load_rows() -> list[OrderRow]:
        return snapshot

    def handler(query: Mapping[str, str]) -> Response:
        return handle_list_orders(query, load_rows)

    return handler


__all__ = ["build_list_orders_handler", "handle_list_orders"]
