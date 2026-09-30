import 'dart:convert';
import 'dart:io';

import 'orders_remote_source.dart';

class HttpOrdersRemoteSource implements OrdersRemoteSource {
  HttpOrdersRemoteSource(this._client, this._ordersUri);

  final HttpClient _client;
  final Uri _ordersUri;

  @override
  Future<List<Map<String, Object?>>> fetchOrderRecords() async {
    final request = await _client.getUrl(_ordersUri);
    request.headers.set(HttpHeaders.acceptHeader, ContentType.json.mimeType);
    final response = await request.close();
    final body = await response.transform(utf8.decoder).join();
    if (response.statusCode != HttpStatus.ok) {
      throw HttpException('GET orders returned ${response.statusCode}', uri: _ordersUri);
    }
    final decoded = jsonDecode(body);
    if (decoded is! List<Object?>) {
      throw FormatException('Expected a JSON array of orders', body);
    }
    return [
      for (final record in decoded)
        if (record is Map<String, Object?>) record else throw FormatException('Expected an order object', record),
    ];
  }
}
