import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/core/domain/orders/order.dart';
import 'package:shop_app/core/domain/orders/order_repository.dart';
import 'package:shop_app/core/domain/orders/orders_unavailable_exception.dart';
import 'package:shop_app/features/orders/presentation/orders_list_holder.dart';
import 'package:shop_app/features/orders/presentation/orders_list_state.dart';

class FakeOrderRepository implements OrderRepository {
  FakeOrderRepository(this._fetch);

  final Future<List<Order>> Function() _fetch;

  @override
  Future<List<Order>> fetchOrders() => _fetch();
}

Order order(String id, {required DateTime placedAt, String customerName = 'Ann', int totalCents = 100}) =>
    Order(id: id, customerName: customerName, totalCents: totalCents, placedAt: placedAt);

void main() {
  test('starts in the loading state', () {
    expect(OrdersListHolder(FakeOrderRepository(() async => [])).state, isA<OrdersLoading>());
  });

  test('shows a row with the formatted total', () async {
    final holder = OrdersListHolder(FakeOrderRepository(() async => [
          order('o1', customerName: 'Ann', totalCents: 1250, placedAt: DateTime.utc(2026, 1, 2)),
        ]));

    await holder.load();

    final row = (holder.state as OrdersLoaded).rows.single;
    expect(row.customerName, 'Ann');
    expect(row.totalLabel, r'$12.50');
  });

  test('formats zero, sub-dollar, whole-dollar and negative totals', () async {
    const expected = {-105: r'-$1.05', -1: r'-$0.01', 0: r'$0.00', 1: r'$0.01', 99: r'$0.99', 100: r'$1.00'};
    for (final MapEntry(key: cents, value: label) in expected.entries) {
      final holder = OrdersListHolder(FakeOrderRepository(() async => [order('o1', totalCents: cents, placedAt: DateTime.utc(2026))]));

      await holder.load();

      expect((holder.state as OrdersLoaded).rows.single.totalLabel, label, reason: '$cents cents');
    }
  });

  test('shows the empty message when there are no orders', () async {
    final holder = OrdersListHolder(FakeOrderRepository(() async => []));

    await holder.load();

    expect(holder.state, isA<OrdersEmpty>().having((state) => state.message, 'message', 'No orders in the last 90 days'));
  });

  test('shows the error state when the orders are unavailable', () async {
    final holder = OrdersListHolder(
      FakeOrderRepository(() async => throw const OrdersUnavailableException()),
    );

    await holder.load();

    expect(holder.state, isA<OrdersError>());
  });

  test('notifies listeners of each state change', () async {
    final holder = OrdersListHolder(FakeOrderRepository(() async => [order('o1', placedAt: DateTime.utc(2026))]));
    final seen = <Type>[];
    holder.addListener(() => seen.add(holder.state.runtimeType));

    await holder.load();

    expect(seen, [OrdersLoading, OrdersLoaded]);
  });

  test('keeps the newest load when an older load finishes last', () async {
    final older = Completer<List<Order>>();
    final newer = Completer<List<Order>>();
    final pending = [older, newer];
    final holder = OrdersListHolder(FakeOrderRepository(() => pending.removeAt(0).future));

    final olderLoad = holder.load();
    final newerLoad = holder.load();
    newer.complete([order('new', placedAt: DateTime.utc(2026, 5, 1))]);
    await newerLoad;
    older.complete([]);
    await olderLoad;

    expect((holder.state as OrdersLoaded).rows.single.id, 'new');
  });
}
