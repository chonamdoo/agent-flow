# Change under QA
`GET /orders` is now served by `app.py` and must require a bearer token.
Spec items:
- SPEC-1: `GET /orders` with `Authorization: Bearer test-token` returns 200 and `{"orders": [...]}`.
- SPEC-2: `GET /orders` without a valid token returns 401 and `{"error": "unauthorized"}`.
Unit tests: `python3 -m unittest discover -s tests -t .` (they call `list_orders` directly).
Start the server with `sh scripts/serve.sh 8765` (foreground; stop it when done). curl is available.
