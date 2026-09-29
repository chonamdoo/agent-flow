from collections.abc import Iterable

from api.orders.list_orders_handler import Handler, make_list_orders_handler
from data.orders.in_memory_order_repository import InMemoryOrderRepository
from data.orders.order_row_mapper import OrderRow
from domain.orders.list_orders import ListOrders


def build_list_orders_handler(rows: Iterable[OrderRow]) -> Handler:
    return make_list_orders_handler(ListOrders(InMemoryOrderRepository(rows)))
