"""orders service: reads orders from Postgres and caches them in Redis.

DB unavailable at startup: retry 5 times, then exit.
Redis unavailable: log a warning and read from Postgres.
"""
import json
import os
import sys
import time
from contextlib import closing

import psycopg2
import redis
from flask import Flask, Response, jsonify

from common import get_logger

log = get_logger("orders")

DB = {
    "host": os.environ.get("DB_HOST", "postgres"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "shop"),
    "user": os.environ.get("DB_USER", "shop"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "connect_timeout": 3,
}
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
PORT = int(os.environ.get("PORT", "5000"))

cache = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=1, socket_timeout=1
)
app = Flask(__name__)

SEED = [("keyboard", 2), ("monitor", 1), ("usb-c cable", 5), ("laptop stand", 1)]


def db_connect():
    return psycopg2.connect(**DB)


def init_db(retries: int = 5) -> None:
    for attempt in range(1, retries + 1):
        try:
            with closing(db_connect()) as conn, conn, conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS orders "
                    "(id SERIAL PRIMARY KEY, item TEXT NOT NULL, qty INT NOT NULL)"
                )
                cur.execute("SELECT count(*) FROM orders")
                if cur.fetchone()[0] == 0:
                    cur.executemany(
                        "INSERT INTO orders (item, qty) VALUES (%s, %s)", SEED
                    )
            log.info("database ready", extra={"fields": {"db_host": DB["host"]}})
            return
        except psycopg2.Error as e:
            log.error(
                f"database connection failed (attempt {attempt}/{retries}): "
                f"{str(e).strip()}",
                extra={"fields": {"db_host": DB["host"], "db_port": DB["port"]}},
            )
            time.sleep(2)
    log.critical("giving up on database, exiting")
    sys.exit(1)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/ready")
def ready():
    try:
        with closing(db_connect()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ready")
    except psycopg2.Error as e:
        log.warning(f"readiness check failed: {str(e).strip()}")
        return jsonify(status="not ready"), 503


@app.get("/orders")
def orders():
    try:
        cached = cache.get("orders")
        if cached:
            return Response(cached, mimetype="application/json")
    except redis.RedisError as e:
        log.warning(
            f"cache unavailable: {e}",
            extra={"fields": {"redis_host": REDIS_HOST, "redis_port": REDIS_PORT}},
        )

    try:
        with closing(db_connect()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, item, qty FROM orders ORDER BY id")
            rows = [{"id": r[0], "item": r[1], "qty": r[2]} for r in cur.fetchall()]
    except psycopg2.Error as e:
        log.error(f"query failed: {str(e).strip()}", extra={"fields": {"db_host": DB["host"]}})
        return jsonify(error="database error"), 503

    body = json.dumps(rows)
    try:
        cache.setex("orders", 5, body)
    except redis.RedisError:
        pass
    return Response(body, mimetype="application/json")


if __name__ == "__main__":
    log.info("starting orders", extra={"fields": {"port": PORT}})
    init_db()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
