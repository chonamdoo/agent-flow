from typing import Any

Response = dict[str, Any]


def ok(body: dict[str, Any]) -> Response:
    return {"status": 200, "body": body}


def bad_request(error: str) -> Response:
    return {"status": 400, "body": {"error": error}}


def not_found(error: str) -> Response:
    return {"status": 404, "body": {"error": error}}
