"""frontend service: proxies requests to orders."""
import os
import time

import requests
from flask import Flask, jsonify

from common import get_logger

log = get_logger("frontend")

ORDERS_URL = os.environ.get("ORDERS_URL", "http://orders:5000")
ORDERS_TIMEOUT = float(os.environ.get("ORDERS_TIMEOUT", "2"))
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)
session = requests.Session()


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def index():
    start = time.monotonic()
    try:
        r = session.get(f"{ORDERS_URL}/orders", timeout=ORDERS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        latency = round((time.monotonic() - start) * 1000)
        log.error(
            f"upstream orders request failed: {type(e).__name__}: {str(e)[:300]}",
            extra={"fields": {"upstream": ORDERS_URL, "latency_ms": latency}},
        )
        return jsonify(error="upstream failure"), 502
    latency = round((time.monotonic() - start) * 1000)
    return jsonify(orders=data, upstream_latency_ms=latency)


if __name__ == "__main__":
    log.info("starting frontend", extra={"fields": {"port": PORT, "orders_url": ORDERS_URL}})
    app.run(host="0.0.0.0", port=PORT, threaded=True)
