abstract interface class OrdersRemoteSource {
  Future<List<Map<String, Object?>>> fetchOrderRecords();
}
