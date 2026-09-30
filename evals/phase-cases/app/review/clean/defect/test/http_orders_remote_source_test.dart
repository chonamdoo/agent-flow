import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shop_app/core/data/orders/http_orders_remote_source.dart';

void main() {
  late HttpServer server;
  late HttpClient client;
  late Uri ordersUri;

  setUp(() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    client = HttpClient();
    ordersUri = Uri.parse('http://${server.address.host}:${server.port}/orders');
  });

  tearDown(() async {
    client.close(force: true);
    await server.close(force: true);
  });

  void respond(int status, String body) {
    server.listen((request) {
      request.response
        ..statusCode = status
        ..headers.contentType = ContentType.json
        ..write(body);
      request.response.close();
    });
  }

  test('returns the order records from GET orders', () async {
    respond(HttpStatus.ok, jsonEncode([
      {'id': 'o1', 'customer': 'Ann', 'total_cents': 1250, 'placed_at': '2026-01-02T00:00:00Z'},
    ]));

    final records = await HttpOrdersRemoteSource(client, ordersUri).fetchOrderRecords();

    expect(records, [
      {'id': 'o1', 'customer': 'Ann', 'total_cents': 1250, 'placed_at': '2026-01-02T00:00:00Z'},
    ]);
  });

  test('throws HttpException for a non-200 response', () async {
    respond(HttpStatus.internalServerError, '{}');

    await expectLater(HttpOrdersRemoteSource(client, ordersUri).fetchOrderRecords(), throwsA(isA<HttpException>()));
  });

  test('throws FormatException when the body is not a list of objects', () async {
    respond(HttpStatus.ok, jsonEncode({'orders': []}));

    await expectLater(HttpOrdersRemoteSource(client, ordersUri).fetchOrderRecords(), throwsA(isA<FormatException>()));
  });
}
