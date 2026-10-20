"""Orders API: GET /orders requires a bearer token."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "test-token"
ORDERS = [{"order_id": "o-1", "total": "12.50"}]


def list_orders(auth: bool) -> tuple[int, dict]:
    if not auth:
        return 401, {"error": "unauthorized"}
    return 200, {"orders": ORDERS}


def _authorized(headers) -> bool:
    return headers.get("Authorization") == f"Bearer {TOKEN}"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/orders":
            self.send_response(404)
            self.end_headers()
            return
        status, body = list_orders(auth=_authorized(self.headers))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
