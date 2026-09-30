import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/orders/orders.dart';

void main() {
  test('decodes order records', () async {
    final repository = OrdersRepository(() async => [
          {'id': 'o7', 'customer': 'Dana', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
        ]);

    final order = (await repository.fetchOrders()).single;

    expect(order.id, 'o7');
    expect(order.customerName, 'Dana');
    expect(order.totalCents, 4200);
    expect(order.placedAt, DateTime.utc(2026, 2, 14, 9, 30));
  });

  test('rejects a record with a missing field', () async {
    final repository = OrdersRepository(() async => [
          {'id': 'o7', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
        ]);

    await expectLater(repository.fetchOrders(), throwsA(isA<FormatException>()));
  });
}
