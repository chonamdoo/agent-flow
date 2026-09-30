part of 'orders.dart';

typedef OrdersFetch = Future<List<Map<String, Object?>>> Function();

class OrdersRepository {
  OrdersRepository(this._fetch);

  final OrdersFetch _fetch;

  Future<List<Order>> fetchOrders() async {
    final records = await _fetch();
    return [for (final record in records) Order.fromRecord(record)];
  }
}
