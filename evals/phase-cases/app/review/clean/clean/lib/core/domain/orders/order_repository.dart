import 'order.dart';

abstract interface class OrderRepository {
  /// Throws `OrdersUnavailableException` when the orders cannot be loaded.
  Future<List<Order>> fetchOrders();
}
