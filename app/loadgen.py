"""loadgen: sends requests to the frontend and logs status and latency."""
import os
import time
from http import HTTPStatus

import requests

from common import get_logger

log = get_logger("loadgen")

TARGET_URL = os.environ.get("TARGET_URL", "http://frontend:8080/")
INTERVAL = float(os.environ.get("INTERVAL", "0.5"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "5"))


def phrase(code: int) -> str:
    try:
        return HTTPStatus(code).phrase.lower()
    except ValueError:
        return "unknown status"


def main() -> None:
    log.info("starting loadgen", extra={"fields": {"target": TARGET_URL}})
    session = requests.Session()
    while True:
        start = time.monotonic()
        try:
            r = session.get(TARGET_URL, timeout=REQUEST_TIMEOUT)
            latency = round((time.monotonic() - start) * 1000)
            fields = {"status": r.status_code, "latency_ms": latency, "error": None}
            if r.ok:
                log.info("request ok", extra={"fields": fields})
            else:
                log.warning(f"request failed: {phrase(r.status_code)}", extra={"fields": fields})
        except requests.exceptions.RequestException as e:
            latency = round((time.monotonic() - start) * 1000)
            log.warning(
                f"request error: {type(e).__name__}",
                extra={"fields": {"status": 0, "latency_ms": latency, "error": type(e).__name__}},
            )
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
