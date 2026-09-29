import '../domain/order.dart';
import '../domain/orders_repository_port.dart';

typedef OrdersFetch = Future<List<Map<String, Object?>>> Function();

class OrdersRepository implements OrdersRepositoryPort {
  OrdersRepository(this._fetch);

  final OrdersFetch _fetch;

  @override
  Future<List<Order>> fetchOrders() async {
    final records = await _fetch();
    return [for (final record in records) Order.fromRecord(record)];
  }
}
