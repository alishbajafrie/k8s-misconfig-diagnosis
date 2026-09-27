"""JSON logging shared by the demo services."""
import json
import logging
import re
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": ANSI.sub("", record.getMessage()).strip(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(entry)


def get_logger(service: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger(service)
