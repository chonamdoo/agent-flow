"""HTTP adapter for the cart quote: JSON body in, (status, body) out."""
from typing import Dict, Mapping, Tuple

from shop.cart.domain import CartLine, InvalidQuantity, UnknownProduct, quote_cart


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def build_cart_quote_handler(catalog: Mapping[str, Dict[str, int]]):
    def handle(body: Dict) -> Tuple[int, Dict]:
        try:
            lines = [CartLine(str(line["product_id"]), int(line["quantity"])) for line in body.get("lines", [])]
            quote = quote_cart(lines, catalog)
        except UnknownProduct as error:
            return 404, {"error": "unknown_product", "product_id": str(error)}
        except (InvalidQuantity, KeyError, TypeError, ValueError):
            return 422, {"error": "invalid_line"}
        return 200, {
            "subtotal": _money(quote.subtotal_cents),
            "shipping": _money(quote.shipping_cents),
            "tax": _money(quote.tax_cents),
            "total": _money(quote.total_cents),
            "free_shipping": quote.free_shipping,
            "item_count_label": quote.item_count_label,
        }

    return handle
