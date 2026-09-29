import '../../domain/orders/order.dart';
import '../../domain/orders/order_repository.dart';
import '../../domain/orders/orders_unavailable_exception.dart';
import 'order_dto.dart';
import 'orders_remote_source.dart';

class OrderRepositoryImpl implements OrderRepository {
  OrderRepositoryImpl(this._remote);

  final OrdersRemoteSource _remote;

  @override
  Future<List<Order>> fetchOrders() async {
    try {
      final records = await _remote.fetchOrderRecords();
      return [for (final record in records) Order.fromDto(OrderDto.fromJson(record))];
    } on Exception {
      throw const OrdersUnavailableException();
    }
  }
}
