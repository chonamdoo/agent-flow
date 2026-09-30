SERVICE_NAME = "orders-service"
SERVICE_VERSION = "0.1.0"


def version_payload() -> dict:
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION}
