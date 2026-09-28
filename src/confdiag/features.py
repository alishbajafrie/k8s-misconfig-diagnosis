"""Feature extraction.

Groups: logs_text, symptom (loadgen stats), k8s_text (events), k8s_state.
No config values and no per-service feature names, so features from one
service can be compared with another.
"""
from __future__ import annotations

import re
from collections import Counter

import numpy as np

from .parse import Run

_MASKS = [
    (re.compile(r"\[\d{2}/[a-z]{3}/\d{4} [\d:]+\]"), " <ts> "),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), " <ip> "),
    (re.compile(r"\b[a-z0-9-]+?-[a-z0-9]{8,10}-[a-z0-9]{5}\b"), " <pod> "),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b"), " <hex> "),
    (re.compile(r"\d+"), " <n> "),
]
_SERVICE_WORDS = re.compile(r"\b(orders|frontend|loadgen|postgres|redis|web|order)\b")


def normalise(text: str, mask_services: bool = True) -> str:
    t = text.lower()
    for rx, repl in _MASKS:
        t = rx.sub(repl, t)
    if mask_services:
        t = _SERVICE_WORDS.sub(" <svc> ", t)
    return " ".join(t.split())


def logs_text(run: Run) -> str:
    lines = []
    for ln in run.logs:
        if ln.app == "loadgen" and ln.msg == "request ok":
            continue
        lines.append(f"{ln.level.lower()} {normalise(ln.msg)}")
    return "\n".join(lines)


def k8s_text(run: Run) -> str:
    lines = []
    for ev in run.events:
        if ev.get("type") != "Warning":
            continue
        lines.append(f"event {ev.get('reason', '')} {normalise(ev.get('message', ''))}")
    return "\n".join(lines)


def symptom_features(run: Run) -> dict[str, float]:
    reqs = [ln for ln in run.logs if ln.app == "loadgen" and "latency_ms" in ln.fields]
    if not reqs:
        return {"lg_requests": 0.0, "lg_error_rate": 0.0, "lg_timeout_rate": 0.0,
                "lg_p50_ms": 0.0, "lg_p95_ms": 0.0}
    lat = np.array([float(ln.fields.get("latency_ms", 0)) for ln in reqs])
    errors = sum(1 for ln in reqs if ln.fields.get("status") != 200)
    timeouts = sum(1 for ln in reqs if "Timeout" in str(ln.fields.get("error") or ""))
    n = len(reqs)
    return {
        "lg_requests": float(n),
        "lg_error_rate": errors / n,
        "lg_timeout_rate": timeouts / n,
        "lg_p50_ms": float(np.percentile(lat, 50)),
        "lg_p95_ms": float(np.percentile(lat, 95)),
    }


def k8s_state_features(run: Run) -> dict[str, float]:
    f: Counter[str] = Counter()
    for pod in run.pods:
        status = pod.get("status", {})
        conds = {c["type"]: c["status"] for c in status.get("conditions", []) or []}
        f["pods_total"] += 1
        if conds.get("Ready") != "True":
            f["pods_not_ready"] += 1
        for cs in status.get("containerStatuses", []) or []:
            f["restarts_total"] += cs.get("restartCount", 0)
            f["restarts_max"] = max(f["restarts_max"], cs.get("restartCount", 0))
            waiting = (cs.get("state") or {}).get("waiting")
            if waiting:
                f[f"waiting_{waiting.get('reason', 'unknown')}"] += 1
            for st in ("state", "lastState"):
                term = (cs.get(st) or {}).get("terminated")
                if term:
                    f[f"terminated_{term.get('reason', 'unknown')}"] += 1

    for ep in run.endpoints:
        subsets = ep.get("subsets") or []
        ready = sum(len(s.get("addresses") or []) for s in subsets)
        not_ready = sum(len(s.get("notReadyAddresses") or []) for s in subsets)
        if ep["metadata"]["name"] == "kubernetes":
            continue
        if ready == 0:
            f["services_no_ready_endpoints"] += 1
        if ready == 0 and not_ready == 0:
            f["services_no_endpoints_at_all"] += 1

    for ev in run.events:
        if ev.get("type") != "Warning":
            continue
        reason = ev.get("reason", "unknown")
        count = ev.get("count") or 1
        f[f"event_{reason}"] += count
        msg = ev.get("message", "")
        if reason == "Unhealthy":
            probe = "liveness" if msg.startswith("Liveness") else "readiness" if msg.startswith("Readiness") else "other"
            kind = ("refused" if "connection refused" in msg
                    else "timeout" if ("deadline exceeded" in msg or "Timeout" in msg)
                    else "http_status" if "statuscode" in msg else "other")
            f[f"probe_{probe}_{kind}"] += count
    return {k: float(v) for k, v in f.items()}


def extract(run: Run) -> dict:
    return {
        "logs_text": logs_text(run),
        "k8s_text": k8s_text(run),
        "symptom": symptom_features(run),
        "k8s_state": k8s_state_features(run),
    }


ABLATIONS = {
    "logs_only": ("logs_text", "symptom"),
    "k8s_only": ("k8s_text", "k8s_state"),
    "logs+k8s": ("logs_text", "symptom", "k8s_text", "k8s_state"),
}
