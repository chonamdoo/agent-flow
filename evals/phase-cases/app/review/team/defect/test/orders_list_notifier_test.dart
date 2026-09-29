import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/features/orders/orders_list_notifier.dart';
import 'package:shop_app/features/orders/orders_list_state.dart';
import 'package:shop_app/features/orders/orders_repository.dart';
import 'package:shop_app/team/api_client.dart';

Map<String, Object?> record(String id, {required String placedAt, String customer = 'Ann', int totalCents = 100}) =>
    {'id': id, 'customer': customer, 'total_cents': totalCents, 'placed_at': placedAt};

OrdersListNotifier notifierOver(JsonTransport transport) => OrdersListNotifier(OrdersRepository(ApiClient(transport)));

OrdersListNotifier notifierReturning(Object? body) => notifierOver((path) async => body);

void main() {
  test('starts in the loading state', () {
    expect(notifierReturning(<Object?>[]).state, isA<OrdersLoading>());
  });

  test('shows rows newest first', () async {
    final notifier = notifierReturning([
      record('o1', customer: 'Ann', totalCents: 1250, placedAt: '2026-01-02T00:00:00Z'),
      record('o2', customer: 'Ben', totalCents: 305, placedAt: '2026-03-09T00:00:00Z'),
      record('o3', customer: 'Cy', totalCents: 99900, placedAt: '2025-12-31T00:00:00Z'),
    ]);

    await notifier.load();

    final rows = (notifier.state as OrdersLoaded).rows;
    expect(rows.map((row) => row.id), ['o2', 'o1', 'o3']);
    expect(rows.map((row) => row.customerName), ['Ben', 'Ann', 'Cy']);
  });

  test('shows the empty message when there are no orders', () async {
    final notifier = notifierReturning(<Object?>[]);

    await notifier.load();

    expect(notifier.state, isA<OrdersEmpty>().having((state) => state.message, 'message', 'No orders in the last 90 days'));
  });

  test('shows the error state for a failed request', () async {
    final notifier = notifierOver((path) async => throw Exception('offline'));

    await notifier.load();

    expect(notifier.state, isA<OrdersError>());
  });

  test('shows the error state for a malformed body', () async {
    final notifier = notifierReturning({'orders': <Object?>[]});

    await notifier.load();

    expect(notifier.state, isA<OrdersError>());
  });

  test('notifies listeners of each state change', () async {
    final notifier = notifierReturning([record('o1', placedAt: '2026-01-02T00:00:00Z')]);
    final seen = <Type>[];
    notifier.addListener(() => seen.add(notifier.state.runtimeType));

    await notifier.load();

    expect(seen, [OrdersLoading, OrdersLoaded]);
  });

  test('keeps the newest load when an older load finishes last', () async {
    final older = Completer<Object?>();
    final newer = Completer<Object?>();
    final pending = [older, newer];
    final notifier = notifierOver((path) => pending.removeAt(0).future);

    final olderLoad = notifier.load();
    final newerLoad = notifier.load();
    newer.complete([record('new', placedAt: '2026-05-01T00:00:00Z')]);
    await newerLoad;
    older.complete(<Object?>[]);
    await olderLoad;

    expect((notifier.state as OrdersLoaded).rows.single.id, 'new');
  });
}
