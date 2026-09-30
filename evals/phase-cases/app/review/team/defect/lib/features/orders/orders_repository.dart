import '../../team/api_client.dart';
import 'order.dart';

class OrdersRepository {
  OrdersRepository(this._client);

  final ApiClient _client;

  Future<List<Order>> fetchOrders() async {
    final body = await _client.getJson('/orders');
    if (body is! List<Object?>) {
      throw FormatException('Expected a JSON array of orders', body);
    }
    return [for (final record in body) _decode(record)];
  }
}

Order _decode(Object? record) {
  if (record
      case {
        'id': final String id,
        'customer': final String customerName,
        'total_cents': final int totalCents,
        'placed_at': final String placedAt,
      }) {
    return Order(
      id: id,
      customerName: customerName,
      totalCents: totalCents,
      placedAt: DateTime.parse(placedAt).toUtc(),
    );
  }
  throw FormatException('Malformed order record', record);
}
