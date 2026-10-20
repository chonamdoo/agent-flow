"""Cart pricing in integer cents. No HTTP, JSON or string money here."""
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

FREE_SHIPPING_THRESHOLD_CENTS = 6000
SHIPPING_CENTS = 2500
MIN_QUANTITY, MAX_QUANTITY = 1, 5


class UnknownProduct(Exception):
    def __init__(self, product_id: str) -> None:
        super().__init__(product_id)
        self.product_id = product_id


class QuantityOutOfRange(Exception):
    def __init__(self, product_id: str) -> None:
        super().__init__(product_id)
        self.product_id = product_id


@dataclass(frozen=True)
class QuotedLine:
    product_id: str
    name: str
    quantity: int
    unit_price_cents: int
    list_price_cents: int
    line_total_cents: int
    discount_rate: int


@dataclass(frozen=True)
class CartQuote:
    lines: Tuple[QuotedLine, ...]
    subtotal_cents: int
    total_discount_cents: int
    shipping_cents: int
    free_shipping_remaining_cents: int
    estimated_total_cents: int


def discount_rate(unit_cents: int, list_cents: int) -> int:
    # Product decision: whole percent, floored.
    return (list_cents - unit_cents) * 100 // list_cents if list_cents else 0


def quote_cart(requested: Sequence[Tuple[str, int]], catalog: Mapping[str, Mapping[str, object]]) -> CartQuote:
    lines = []
    for product_id, quantity in requested:
        product = catalog.get(product_id)
        if product is None:
            raise UnknownProduct(product_id)
        if not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
            raise QuantityOutOfRange(product_id)
        unit, listed = int(product["price_cents"]), int(product["list_price_cents"])
        lines.append(QuotedLine(product_id, str(product["name"]), quantity, unit, listed, unit * quantity,
                                discount_rate(unit, listed)))
    subtotal = sum(line.line_total_cents for line in lines)
    discount = sum((line.list_price_cents - line.unit_price_cents) * line.quantity for line in lines)
    shipping = 0 if subtotal >= FREE_SHIPPING_THRESHOLD_CENTS else SHIPPING_CENTS
    remaining = max(0, FREE_SHIPPING_THRESHOLD_CENTS - subtotal)
    return CartQuote(tuple(lines), subtotal, discount, shipping, remaining, subtotal + shipping)
