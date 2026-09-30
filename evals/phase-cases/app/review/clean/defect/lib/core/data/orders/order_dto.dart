class OrderDto {
  const OrderDto({
    required this.id,
    required this.customer,
    required this.totalCents,
    required this.placedAt,
  });

  factory OrderDto.fromJson(Map<String, Object?> json) {
    if (json
        case {
          'id': final String id,
          'customer': final String customer,
          'total_cents': final int totalCents,
          'placed_at': final String placedAt,
        }) {
      return OrderDto(id: id, customer: customer, totalCents: totalCents, placedAt: placedAt);
    }
    throw FormatException('Malformed order record', json);
  }

  final String id;
  final String customer;
  final int totalCents;
  final String placedAt;
}
