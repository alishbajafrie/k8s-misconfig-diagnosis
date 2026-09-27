"""Load a run directory."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

POD_HASH = re.compile(r"^(?P<app>[a-z0-9-]+?)-[a-z0-9]{8,10}-[a-z0-9]{5}$")


@dataclass
class LogLine:
    app: str
    pod: str
    container: str
    previous: bool
    level: str
    msg: str
    fields: dict = field(default_factory=dict)


@dataclass
class Run:
    path: Path
    meta: dict
    pods: list[dict]
    services: list[dict]
    endpoints: list[dict]
    deployments: list[dict]
    netpols: list[dict]
    events: list[dict]
    logs: list[LogLine]

    @property
    def label(self) -> str:
        return self.meta.get("label", "unknown")

    @property
    def variant(self) -> str:
        return self.meta.get("variant", "a")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_time(ev: dict) -> datetime | None:
    for key in ("lastTimestamp", "eventTime", "firstTimestamp"):
        t = parse_ts(ev.get(key))
        if t:
            return t
    return parse_ts(ev.get("metadata", {}).get("creationTimestamp"))


def app_of_pod(pod_name: str, pods_by_name: dict[str, dict]) -> str:
    pod = pods_by_name.get(pod_name)
    if pod:
        app = pod.get("metadata", {}).get("labels", {}).get("app")
        if app:
            return app
    m = POD_HASH.match(pod_name)
    return m.group("app") if m else pod_name


def _items(path: Path) -> list[dict]:
    if not path.exists() or not path.read_text().strip():
        return []
    return json.loads(path.read_text()).get("items", [])


def parse_log_file(path: Path, pods_by_name: dict[str, dict]) -> list[LogLine]:
    stem = path.name[: -len(".log")]
    parts = stem.split("__")
    pod, container = parts[0], parts[1] if len(parts) > 1 else "unknown"
    previous = len(parts) > 2 and parts[2] == "previous"
    app = app_of_pod(pod, pods_by_name)
    out = []
    for raw in path.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError
            msg = str(obj.pop("msg", ""))
            level = str(obj.pop("level", "INFO"))
            out.append(LogLine(app, pod, container, previous, level, msg, obj))
        except ValueError:
            out.append(LogLine(app, pod, container, previous, "RAW", raw))
    return out


def load_run(path: Path) -> Run:
    path = Path(path)
    meta = json.loads((path / "meta.json").read_text())
    pods = _items(path / "pods.json")
    pods_by_name = {p["metadata"]["name"]: p for p in pods}

    since = parse_ts(meta.get("inject_time"))
    events = []
    for ev in _items(path / "events.json"):
        t = event_time(ev)
        if since is None or t is None or t >= since - timedelta(seconds=2):
            events.append(ev)

    logs: list[LogLine] = []
    logdir = path / "logs"
    if logdir.exists():
        for f in sorted(logdir.glob("*.log")):
            logs.extend(parse_log_file(f, pods_by_name))

    return Run(
        path=path,
        meta=meta,
        pods=pods,
        services=_items(path / "services.json"),
        endpoints=_items(path / "endpoints.json"),
        deployments=_items(path / "deployments.json"),
        netpols=_items(path / "networkpolicies.json"),
        events=events,
        logs=logs,
    )


def find_runs(root: Path) -> list[Path]:
    root = Path(root)
    if (root / "meta.json").exists():
        return [root]
    return sorted(p.parent for p in root.rglob("meta.json"))
