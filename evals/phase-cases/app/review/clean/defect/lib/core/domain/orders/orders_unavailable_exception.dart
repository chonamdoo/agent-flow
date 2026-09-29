class OrdersUnavailableException implements Exception {
  const OrdersUnavailableException();

  @override
  String toString() => 'OrdersUnavailableException: orders are unavailable';
}
