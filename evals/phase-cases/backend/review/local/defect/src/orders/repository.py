from collections.abc import Iterable, Mapping
from typing import Callable, Protocol, Union

OrderRow = Mapping[str, Union[str, int]]


class OrderRepository(Protocol):
    def rows_for_customer(self, customer_id: str) -> list[OrderRow]: ...


class CallableOrderRepository:
    def __init__(self, load_rows: Callable[[], Iterable[OrderRow]]) -> None:
        self._load_rows = load_rows

    def rows_for_customer(self, customer_id: str) -> list[OrderRow]:
        return [row for row in self._load_rows() if str(row["customer_id"]) == customer_id]
