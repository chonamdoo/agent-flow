import '../lib/core/domain/orders/order.dart';
import '../lib/core/domain/orders/order_repository.dart';
import '../lib/features/orders/presentation/orders_list_holder.dart';
import '../lib/features/orders/presentation/orders_list_state.dart';

class FakeOrderRepository implements OrderRepository {
  FakeOrderRepository(this._result);

  final Future<List<Order>> Function() _result;

  @override
  Future<List<Order>> fetchOrders() => _result();
}

void check(bool condition, String message) {
  if (!condition) throw StateError('FAILED: $message');
}

Future<void> main() async {
  final idle = OrdersListHolder(FakeOrderRepository(() async => []));
  check(idle.state is OrdersLoading, 'state is loading before load() completes');

  final holder = OrdersListHolder(FakeOrderRepository(() async => [
        Order(id: 'o1', customerName: 'Ann', totalCents: 1250, placedAt: DateTime.utc(2026, 1, 2)),
        Order(id: 'o2', customerName: 'Ben', totalCents: 305, placedAt: DateTime.utc(2026, 3, 9)),
        Order(id: 'o3', customerName: 'Cy', totalCents: 99900, placedAt: DateTime.utc(2025, 12, 31)),
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

  final empty = OrdersListHolder(FakeOrderRepository(() async => []));
  await empty.load();
  check(empty.state is OrdersEmpty, 'no orders produce the empty state');

  final failing = OrdersListHolder(FakeOrderRepository(() async => throw Exception('offline')));
  await failing.load();
  check(failing.state is OrdersError, 'repository failure produces the error state');

  print('orders_list_holder_test: all passed');
}
