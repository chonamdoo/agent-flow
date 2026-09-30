part of 'orders.dart';

class OrderRow {
  const OrderRow({required this.id, required this.customerName, required this.totalLabel});

  final String id;
  final String customerName;
  final String totalLabel;
}

sealed class OrdersListState {
  const OrdersListState();
}

final class OrdersLoading extends OrdersListState {
  const OrdersLoading();
}

final class OrdersLoaded extends OrdersListState {
  const OrdersLoaded(this.rows);

  final List<OrderRow> rows;
}

final class OrdersEmpty extends OrdersListState {
  const OrdersEmpty(this.message);

  final String message;
}

final class OrdersError extends OrdersListState {
  const OrdersError(this.message);

  final String message;
}
