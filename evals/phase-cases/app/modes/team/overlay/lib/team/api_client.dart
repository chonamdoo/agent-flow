/// Shared HTTP facade. Features receive an instance; tests pass a fake transport.
typedef JsonTransport = Future<Object?> Function(String path);

class ApiClient {
  ApiClient(this._transport);

  final JsonTransport _transport;

  /// Returns the decoded JSON body for `GET <path>`.
  Future<Object?> getJson(String path) => _transport(path);
}
