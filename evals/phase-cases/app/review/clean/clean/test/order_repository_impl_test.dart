import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/core/data/orders/order_repository_impl.dart';
import 'package:shop_app/core/data/orders/orders_remote_source.dart';
import 'package:shop_app/core/domain/orders/orders_unavailable_exception.dart';

class FakeOrdersRemoteSource implements OrdersRemoteSource {
  FakeOrdersRemoteSource(this._fetch);

  final Future<List<Map<String, Object?>>> Function() _fetch;

  @override
  Future<List<Map<String, Object?>>> fetchOrderRecords() => _fetch();
}

void main() {
  test('maps order records to domain orders', () async {
    final repository = OrderRepositoryImpl(FakeOrdersRemoteSource(() async => [
          {'id': 'o7', 'customer': 'Dana', 'total_cents': 4200, 'placed_at': '2026-02-14T09:30:00Z'},
        ]));

    final order = (await repository.fetchOrders()).single;

    expect(order.id, 'o7');
    expect(order.customerName, 'Dana');
    expect(order.totalCents, 4200);
    expect(order.placedAt, DateTime.utc(2026, 2, 14, 9, 30));
  });

  test('reports a malformed record as unavailable orders', () async {
    final repository = OrderRepositoryImpl(FakeOrdersRemoteSource(() async => [
          {'id': 'o7', 'customer': 'Dana', 'total_cents': '42.00', 'placed_at': '2026-02-14T09:30:00Z'},
        ]));

    await expectLater(repository.fetchOrders(), throwsA(isA<OrdersUnavailableException>()));
  });

  test('reports a transport failure as unavailable orders', () async {
    final repository = OrderRepositoryImpl(FakeOrdersRemoteSource(() async => throw Exception('offline')));

    await expectLater(repository.fetchOrders(), throwsA(isA<OrdersUnavailableException>()));
  });
}
