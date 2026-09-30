part of 'orders.dart';

class Order {
  const Order({
    required this.id,
    required this.customerName,
    required this.totalCents,
    required this.placedAt,
  });

  factory Order.fromRecord(Map<String, Object?> record) {
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

  final String id;
  final String customerName;
  final int totalCents;
  final DateTime placedAt;
}
