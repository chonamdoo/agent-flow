import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/orders/orders.dart';

Map<String, Object?> record(String id, {required String placedAt, String customer = 'Ann', int totalCents = 100}) =>
    {'id': id, 'customer': customer, 'total_cents': totalCents, 'placed_at': placedAt};

OrdersListHolder holderReturning(List<Map<String, Object?>> records) =>
    OrdersListHolder(OrdersRepository(() async => records));

void main() {
  test('starts in the loading state', () {
    expect(holderReturning([]).state, isA<OrdersLoading>());
  });

  test('shows rows newest first with formatted totals', () async {
    final holder = holderReturning([
      record('o1', customer: 'Ann', totalCents: 1250, placedAt: '2026-01-02T00:00:00Z'),
      record('o2', customer: 'Ben', totalCents: 305, placedAt: '2026-03-09T00:00:00Z'),
      record('o3', customer: 'Cy', totalCents: 99900, placedAt: '2025-12-31T00:00:00Z'),
    ]);

    await holder.load();

    final rows = (holder.state as OrdersLoaded).rows;
    expect(rows.map((row) => row.id), ['o2', 'o1', 'o3']);
    expect(rows.map((row) => row.customerName), ['Ben', 'Ann', 'Cy']);
    expect(rows.map((row) => row.totalLabel), [r'$3.05', r'$12.50', r'$999.00']);
  });

  test('formats zero, sub-dollar, whole-dollar and negative totals', () async {
    const expected = {-105: r'-$1.05', -1: r'-$0.01', 0: r'$0.00', 1: r'$0.01', 99: r'$0.99', 100: r'$1.00'};
    for (final MapEntry(key: cents, value: label) in expected.entries) {
      final holder = holderReturning([record('o1', totalCents: cents, placedAt: '2026-01-02T00:00:00Z')]);

      await holder.load();

      expect((holder.state as OrdersLoaded).rows.single.totalLabel, label, reason: '$cents cents');
    }
  });

  test('shows the empty message when there are no orders', () async {
    final holder = holderReturning([]);

    await holder.load();

    expect(holder.state, isA<OrdersEmpty>().having((state) => state.message, 'message', 'No orders in the last 90 days'));
  });

  test('shows the error state when the fetch fails', () async {
    final holder = OrdersListHolder(OrdersRepository(() async => throw Exception('offline')));

    await holder.load();

    expect(holder.state, isA<OrdersError>());
  });

  test('shows the error state for a malformed record', () async {
    final holder = holderReturning([
      {'id': 'o1', 'customer': 'Ann', 'total_cents': '12.50', 'placed_at': '2026-01-02T00:00:00Z'},
    ]);

    await holder.load();

    expect(holder.state, isA<OrdersError>());
  });

  test('notifies listeners of each state change', () async {
    final holder = holderReturning([record('o1', placedAt: '2026-01-02T00:00:00Z')]);
    final seen = <Type>[];
    holder.addListener(() => seen.add(holder.state.runtimeType));

    await holder.load();

    expect(seen, [OrdersLoading, OrdersLoaded]);
  });

  test('keeps the newest load when an older load finishes last', () async {
    final older = Completer<List<Map<String, Object?>>>();
    final newer = Completer<List<Map<String, Object?>>>();
    final pending = [older, newer];
    final holder = OrdersListHolder(OrdersRepository(() => pending.removeAt(0).future));

    final olderLoad = holder.load();
    final newerLoad = holder.load();
    newer.complete([record('new', placedAt: '2026-05-01T00:00:00Z')]);
    await newerLoad;
    older.complete([]);
    await olderLoad;

    expect((holder.state as OrdersLoaded).rows.single.id, 'new');
  });
}
