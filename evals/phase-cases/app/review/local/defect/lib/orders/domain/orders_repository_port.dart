import 'order.dart';

abstract interface class OrdersRepositoryPort {
  Future<List<Order>> fetchOrders();
}
