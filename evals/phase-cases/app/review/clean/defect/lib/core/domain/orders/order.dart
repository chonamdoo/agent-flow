import '../../data/orders/order_dto.dart';

class Order {
  const Order({
    required this.id,
    required this.customerName,
    required this.totalCents,
    required this.placedAt,
  });

  factory Order.fromDto(OrderDto dto) => Order(
        id: dto.id,
        customerName: dto.customer,
        totalCents: dto.totalCents,
        placedAt: DateTime.parse(dto.placedAt).toUtc(),
      );

  final String id;
  final String customerName;
  final int totalCents;
  final DateTime placedAt;
}
