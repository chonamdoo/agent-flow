from collections.abc import Iterable, Mapping
from typing import Callable, Union

from app.inventory_http import handle_stock_level
from app.responses import Response

StoredRow = Mapping[str, Union[str, int]]
Handler = Callable[[Mapping[str, str]], Response]


def build_stock_level_handler(rows: Iterable[StoredRow]) -> Handler:
    levels: dict[str, int] = {str(row["sku"]): int(row["on_hand"]) for row in rows}

    def load_levels() -> dict[str, int]:
        return levels

    def handler(query: Mapping[str, str]) -> Response:
        return handle_stock_level(query, load_levels)

    return handler
