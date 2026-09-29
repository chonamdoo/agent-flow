from collections.abc import Iterable, Mapping
from typing import Callable, Union

from app.inventory_http import handle_stock_level
from app.orders_http import handle_list_orders
from app.responses import Response
from domain.orders.models import OrderRowDTO

StoredRow = Mapping[str, Union[str, int]]
Handler = Callable[[Mapping[str, str]], Response]


def build_stock_level_handler(rows: Iterable[StoredRow]) -> Handler:
    levels: dict[str, int] = {str(row["sku"]): int(row["on_hand"]) for row in rows}

    def load_levels() -> dict[str, int]:
        return levels

    def handler(query: Mapping[str, str]) -> Response:
        return handle_stock_level(query, load_levels)

    return handler


def _order_row(row: StoredRow) -> OrderRowDTO:
    return OrderRowDTO(
        order_id=str(row["order_id"]),
        customer_id=str(row["customer_id"]),
        status=str(row["status"]),
        total_cents=int(row["total_cents"]),
        placed_at=str(row["placed_at"]),
    )


def build_list_orders_handler(rows: Iterable[StoredRow]) -> Handler:
    stored: tuple[OrderRowDTO, ...] = tuple(_order_row(row) for row in rows)

    def load_rows() -> tuple[OrderRowDTO, ...]:
        return stored

    def handler(query: Mapping[str, str]) -> Response:
        return handle_list_orders(query, load_rows)

    return handler
