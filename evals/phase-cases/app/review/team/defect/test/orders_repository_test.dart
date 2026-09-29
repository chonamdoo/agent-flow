import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/features/orders/orders_repository.dart';
import 'package:shop_app/team/api_client.dart';

void main() {
  test('requests GET /orders and decodes order records', () async {
    final paths = <String>[];
    final repository = OrdersRepository(ApiClient((path) async {
      paths.add(path);
      return [
        {'id': 'o7', 'customer': 'Dana', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
      ];
    }));

    final order = (await repository.fetchOrders()).single;

    expect(paths, ['/orders']);
    expect(order.id, 'o7');
    expect(order.customerName, 'Dana');
    expect(order.totalCents, 4200);
    expect(order.placedAt, DateTime.utc(2026, 2, 14, 9, 30));
  });

  test('rejects a body that is not a list', () async {
    final repository = OrdersRepository(ApiClient((path) async => {'orders': <Object?>[]}));

    await expectLater(repository.fetchOrders(), throwsA(isA<FormatException>()));
  });

  test('rejects a record with a missing field', () async {
    final repository = OrdersRepository(ApiClient((path) async => [
          {'id': 'o7', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
        ]));

    await expectLater(repository.fetchOrders(), throwsA(isA<FormatException>()));
  });
}
