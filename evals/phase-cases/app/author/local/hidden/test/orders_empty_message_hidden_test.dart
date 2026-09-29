import '../lib/orders/orders.dart';

Future<void> main() async {
  final holder = OrdersListHolder(OrdersRepository(() async => []));
  await holder.load();
  final state = holder.state;
  if (state is! OrdersEmpty) throw StateError('FAILED: expected the empty state');
  if (state.message != 'No orders in the last 90 days') {
    throw StateError('FAILED: empty-state message was "${state.message}"');
  }
  print('orders_empty_message_hidden_test: passed');
}
