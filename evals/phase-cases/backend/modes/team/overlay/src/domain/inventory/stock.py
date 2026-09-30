LOW_STOCK_THRESHOLD = 5


def stock_state(on_hand: int) -> str:
    return "low" if on_hand < LOW_STOCK_THRESHOLD else "in_stock"
