"""Cart pricing. Pure: no HTTP, no JSON, money in integer cents."""
from dataclasses import dataclass
from typing import Dict, List, Mapping

FREE_SHIPPING_THRESHOLD_CENTS = 6000
SHIPPING_FEE_CENTS = 500
MAX_QUANTITY = 5


class UnknownProduct(Exception):
    pass


class InvalidQuantity(Exception):
    pass


@dataclass(frozen=True)
class CartLine:
    product_id: str
    quantity: int


@dataclass(frozen=True)
class Quote:
    subtotal_cents: int
    shipping_cents: int
    total_cents: int
    free_shipping: bool
    item_count_label: str
    discount_cents: int = 0


def quote_cart(lines: List[CartLine], catalog: Mapping[str, Dict[str, int]]) -> Quote:
    subtotal = 0
    for line in lines:
        if line.product_id not in catalog:
            raise UnknownProduct(line.product_id)
        if not 1 <= line.quantity <= MAX_QUANTITY:
            raise InvalidQuantity(line.quantity)
        subtotal += catalog[line.product_id]["price_cents"] * line.quantity
    free = subtotal >= FREE_SHIPPING_THRESHOLD_CENTS
    shipping = 0 if free else SHIPPING_FEE_CENTS
    count = sum(line.quantity for line in lines)
    label = "(1 item)" if count == 1 else f"({count} items)"
    discount = subtotal // 10 if subtotal >= 10000 else 0
    return Quote(subtotal, shipping, subtotal - discount + shipping, free, label, discount)
