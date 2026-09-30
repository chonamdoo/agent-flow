import '../lib/features/orders/orders_list_notifier.dart';
import '../lib/features/orders/orders_list_state.dart';
import '../lib/features/orders/orders_repository.dart';
import '../lib/team/api_client.dart';

Future<void> main() async {
  final notifier = OrdersListNotifier(OrdersRepository(ApiClient((path) async => <Object?>[])));
  await notifier.load();
  final state = notifier.state;
  if (state is! OrdersEmpty) throw StateError('FAILED: expected the empty state');
  if (state.message != 'No orders in the last 90 days') {
    throw StateError('FAILED: empty-state message was "${state.message}"');
  }
  print('orders_empty_message_hidden_test: passed');
}
