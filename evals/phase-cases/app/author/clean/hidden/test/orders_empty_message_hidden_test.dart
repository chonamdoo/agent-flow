import '../lib/core/domain/orders/order.dart';
import '../lib/core/domain/orders/order_repository.dart';
import '../lib/features/orders/presentation/orders_list_holder.dart';
import '../lib/features/orders/presentation/orders_list_state.dart';

class EmptyOrderRepository implements OrderRepository {
  @override
  Future<List<Order>> fetchOrders() async => [];
}

Future<void> main() async {
  final holder = OrdersListHolder(EmptyOrderRepository());
  await holder.load();
  final state = holder.state;
  if (state is! OrdersEmpty) throw StateError('FAILED: expected the empty state');
  if (state.message != 'No orders in the last 90 days') {
    throw StateError('FAILED: empty-state message was "${state.message}"');
  }
  print('orders_empty_message_hidden_test: passed');
}
