import '../lib/features/orders/orders_repository.dart';
import '../lib/team/api_client.dart';

void check(bool condition, String message) {
  if (!condition) throw StateError('FAILED: $message');
}

Future<void> main() async {
  final requested = <String>[];
  final repository = OrdersRepository(ApiClient((path) async {
    requested.add(path);
    return [
      {'id': 'o7', 'customer': 'Dana', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
    ];
  }));
  final orders = await repository.fetchOrders();
  check(requested.join(',') == '/orders', 'repository requests GET /orders');
  check(orders.length == 1, 'one record maps to one order');
  final order = orders.single;
  check(order.id == 'o7', 'id is mapped');
  check(order.customerName == 'Dana', 'customer is mapped to customerName');
  check(order.totalCents == 4200, 'total_cents is mapped to totalCents');
  check(order.placedAt == DateTime.utc(2026, 2, 14, 9, 30), 'placed_at is parsed as a UTC instant');

  print('orders_repository_test: all passed');
}
