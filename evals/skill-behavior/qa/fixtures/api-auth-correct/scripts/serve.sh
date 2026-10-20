#!/bin/sh
# Start the API in the foreground: sh scripts/serve.sh [port]
exec python3 app.py --port "${1:-8765}"
