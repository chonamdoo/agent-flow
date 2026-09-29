class Order {
  const Order({
    required this.id,
    required this.customerName,
    required this.totalCents,
    required this.placedAt,
  });

  final String id;
  final String customerName;
  final int totalCents;
  final DateTime placedAt;
}
