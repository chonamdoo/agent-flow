from collections.abc import Mapping
from typing import Callable

from app.responses import Response, bad_request, not_found, ok
from domain.inventory.stock import stock_state

LoadLevels = Callable[[], Mapping[str, int]]


def handle_stock_level(query: Mapping[str, str], load_levels: LoadLevels) -> Response:
    sku = query.get("sku")
    if not sku:
        return bad_request("missing_sku")
    levels = load_levels()
    if sku not in levels:
        return not_found("unknown_sku")
    on_hand = levels[sku]
    return ok({"sku": sku, "on_hand": on_hand, "state": stock_state(on_hand)})
