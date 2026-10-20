"""POST /api/cart/quote adapter: JSON in, (status, JSON) out, money as two-decimal strings."""
from typing import Callable, Dict, Mapping, Tuple

from shop.cart.domain import (FREE_SHIPPING_THRESHOLD_CENTS, QuantityOutOfRange, UnknownProduct,
                              quote_cart)


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def build_cart_quote_handler(catalog: Mapping[str, Mapping[str, object]]) -> Callable[[dict], Tuple[int, Dict]]:
    def handle(body: dict) -> Tuple[int, Dict]:
        lines = body.get("lines") if isinstance(body, dict) else None
        if not isinstance(lines, list):
            return 400, {"error": "malformed_request"}
        try:
            requested = [(str(line["product_id"]), int(line["quantity"])) for line in lines]
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "malformed_request"}
        try:
            quote = quote_cart(requested, catalog)
        except UnknownProduct as error:
            return 404, {"error": "unknown_product", "product_id": error.product_id}
        except QuantityOutOfRange as error:
            return 422, {"error": "quantity_out_of_range", "product_id": error.product_id}
        return 200, {
            "currency": "USD",
            "lines": [{
                "product_id": line.product_id, "name": line.name, "quantity": line.quantity,
                "unit_price": money(line.unit_price_cents), "list_price": money(line.list_price_cents),
                "line_total": money(line.line_total_cents), "discount_rate": line.discount_rate,
            } for line in quote.lines],
            "subtotal": money(quote.subtotal_cents),
            "total_discount": money(quote.total_discount_cents),
            "shipping": money(quote.shipping_cents),
            "free_shipping_threshold": money(FREE_SHIPPING_THRESHOLD_CENTS),
            "free_shipping_remaining": money(quote.free_shipping_remaining_cents),
            "estimated_total": money(quote.estimated_total_cents),
        }

    return handle
