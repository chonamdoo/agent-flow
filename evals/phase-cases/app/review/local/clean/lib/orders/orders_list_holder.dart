part of 'orders.dart';

const String ordersEmptyMessage = 'No orders in the last 90 days';
const String ordersErrorMessage = 'Could not load orders.';

class OrdersListHolder {
  OrdersListHolder(this._repository);

  final OrdersRepository _repository;
  final List<void Function()> _listeners = [];
  OrdersListState _state = const OrdersLoading();
  int _latestLoad = 0;

  OrdersListState get state => _state;

  void addListener(void Function() listener) => _listeners.add(listener);

  void removeListener(void Function() listener) => _listeners.remove(listener);

  Future<void> load() async {
    final loadId = ++_latestLoad;
    _emit(const OrdersLoading());
    final next = await _fetchState();
    if (loadId == _latestLoad) _emit(next);
  }

  Future<OrdersListState> _fetchState() async {
    try {
      final orders = await _repository.fetchOrders();
      return orders.isEmpty ? const OrdersEmpty(ordersEmptyMessage) : OrdersLoaded(_toRows(orders));
    } on Exception {
      return const OrdersError(ordersErrorMessage);
    }
  }

  void _emit(OrdersListState state) {
    _state = state;
    for (final listener in List.of(_listeners)) {
      listener();
    }
  }
}

List<OrderRow> _toRows(List<Order> orders) {
  final newestFirst = [...orders]..sort((a, b) => b.placedAt.compareTo(a.placedAt));
  return [
    for (final order in newestFirst)
      OrderRow(id: order.id, customerName: order.customerName, totalLabel: _formatCents(order.totalCents)),
  ];
}

String _formatCents(int cents) {
  final sign = cents < 0 ? '-' : '';
  final magnitude = cents.abs();
  return '$sign\$${magnitude ~/ 100}.${(magnitude % 100).toString().padLeft(2, '0')}';
}
