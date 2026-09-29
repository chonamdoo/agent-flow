import '../lib/orders/orders.dart';

void check(bool condition, String message) {
  if (!condition) throw StateError('FAILED: $message');
}

Future<void> main() async {
  final idle = OrdersListHolder(OrdersRepository(() async => []));
  check(idle.state is OrdersLoading, 'state is loading before load() completes');

  final holder = OrdersListHolder(OrdersRepository(() async => [
        {'id': 'o1', 'customer': 'Ann', 'total_cents': 1250, 'placed_at': '2026-01-02T00:00:00Z'},
        {'id': 'o2', 'customer': 'Ben', 'total_cents': 305, 'placed_at': '2026-03-09T00:00:00Z'},
        {'id': 'o3', 'customer': 'Cy', 'total_cents': 99900, 'placed_at': '2025-12-31T00:00:00Z'},
      ]));
  await holder.load();
  final loaded = holder.state;
  check(loaded is OrdersLoaded, 'orders produce a loaded state');
  final rows = (loaded as OrdersLoaded).rows;
  check(rows.map((row) => row.id).join(',') == 'o2,o1,o3', 'rows are sorted newest first');
  check(rows[1].customerName == 'Ann', 'row keeps the customer name');
  check(rows[0].totalLabel == '\$3.05', 'total label formats cents');
  check(rows[1].totalLabel == '\$12.50', 'total label keeps two decimals');
  check(rows[2].totalLabel == '\$999.00', 'total label for whole amounts');

  final empty = OrdersListHolder(OrdersRepository(() async => []));
  await empty.load();
  check(empty.state is OrdersEmpty, 'no orders produce the empty state');

  final failing = OrdersListHolder(OrdersRepository(() async => throw Exception('offline')));
  await failing.load();
  check(failing.state is OrdersError, 'fetch failure produces the error state');

  print('orders_list_holder_test: all passed');
}
