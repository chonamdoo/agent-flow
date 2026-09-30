import '../lib/features/orders/orders_list_notifier.dart';
import '../lib/features/orders/orders_list_state.dart';
import '../lib/features/orders/orders_repository.dart';
import '../lib/team/api_client.dart';

void check(bool condition, String message) {
  if (!condition) throw StateError('FAILED: $message');
}

OrdersRepository repositoryReturning(Object? body) => OrdersRepository(ApiClient((path) async => body));

Future<void> main() async {
  final idle = OrdersListNotifier(repositoryReturning(<Object?>[]));
  check(idle.state is OrdersLoading, 'state is loading before load() completes');

  final notifier = OrdersListNotifier(repositoryReturning([
    {'id': 'o1', 'customer': 'Ann', 'total_cents': 1250, 'placed_at': '2026-01-02T00:00:00Z'},
    {'id': 'o2', 'customer': 'Ben', 'total_cents': 305, 'placed_at': '2026-03-09T00:00:00Z'},
    {'id': 'o3', 'customer': 'Cy', 'total_cents': 99900, 'placed_at': '2025-12-31T00:00:00Z'},
  ]));
  await notifier.load();
  final loaded = notifier.state;
  check(loaded is OrdersLoaded, 'orders produce a loaded state');
  final rows = (loaded as OrdersLoaded).rows;
  check(rows.map((row) => row.id).join(',') == 'o2,o1,o3', 'rows are sorted newest first');
  check(rows[1].customerName == 'Ann', 'row keeps the customer name');
  check(rows[0].totalLabel == '\$3.05', 'total label formats cents');
  check(rows[1].totalLabel == '\$12.50', 'total label keeps two decimals');
  check(rows[2].totalLabel == '\$999.00', 'total label for whole amounts');

  final empty = OrdersListNotifier(repositoryReturning(<Object?>[]));
  await empty.load();
  check(empty.state is OrdersEmpty, 'no orders produce the empty state');

  final failing = OrdersListNotifier(OrdersRepository(ApiClient((path) async => throw Exception('offline'))));
  await failing.load();
  check(failing.state is OrdersError, 'request failure produces the error state');

  print('orders_list_notifier_test: all passed');
}
