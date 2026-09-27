"""Offline run builder for tests.

Applies the fault commands from faults.yaml to the base manifests and adds
approximate symptoms. Used only for testing code paths.
"""
from __future__ import annotations

import copy
import json
import random
import re
import shlex
from pathlib import Path

import jsonpatch
import yaml

from confdiag.catalog import REPO_ROOT, load_catalog

POD_SUFFIX = "5d8f7c9b4-x2k9p"


def load_manifests() -> list[dict]:
    docs = []
    for f in sorted((REPO_ROOT / "k8s" / "base").glob("*.yaml")):
        docs.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
    return docs


def _find(docs, kind, name):
    for d in docs:
        if d["kind"].lower() == kind.lower() and d["metadata"]["name"] == name:
            return d
    raise KeyError(f"{kind}/{name} not in manifests")


def _container(dep, name):
    for c in dep["spec"]["template"]["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise KeyError(f"container {name} not in {dep['metadata']['name']}")


def apply_command(docs: list[dict], cmd: str) -> None:
    args = shlex.split(cmd)
    assert args[0] == "kubectl", cmd
    if "-n" in args:
        i = args.index("-n")
        args = args[:i] + args[i + 2:]
    verb = args[1]
    if verb == "patch":
        kind, name = args[2], args[3]
        ptype = args[args.index("--type=json") if "--type=json" in args else args.index("--type=merge")]
        patch = json.loads(args[args.index("-p") + 1])
        obj = _find(docs, kind, name)
        if ptype == "--type=json":
            new = jsonpatch.apply_patch(obj, patch)
            obj.clear()
            obj.update(new)
        else:
            for k, v in patch.items():
                obj.setdefault(k, {}).update(v)
    elif verb == "set":
        what, target = args[2], args[3]
        kind, name = target.split("/")
        dep = _find(docs, kind, name)
        if what == "env":
            for kv in args[4:]:
                k, v = kv.split("=", 1)
                c = dep["spec"]["template"]["spec"]["containers"][0]
                env = next(e for e in c["env"] if e["name"] == k)
                env["value"] = v
        elif what == "image":
            cname, image = args[4].split("=", 1)
            _container(dep, cname)["image"] = image
        elif what == "resources":
            cname = args[args.index("-c") + 1]
            c = _container(dep, cname)
            for flag in ("--requests", "--limits"):
                for a in args:
                    if a.startswith(flag + "="):
                        res, val = a.split("=", 1)[1].split("=")
                        c["resources"][flag[2:]][res] = val
        else:
            raise ValueError(cmd)
    elif verb == "apply":
        path = REPO_ROOT / args[args.index("-f") + 1]
        docs.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    elif verb == "rollout":
        pass
    else:
        raise ValueError(f"unsupported command in simulator: {cmd}")


def _pods(docs):
    pods = []
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        tpl = d["spec"]["template"]
        pods.append({
            "metadata": {"name": f"{d['metadata']['name']}-{POD_SUFFIX}",
                         "labels": dict(tpl["metadata"]["labels"])},
            "spec": copy.deepcopy(tpl["spec"]),
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": c["name"], "image": c["image"], "restartCount": 0,
                                       "state": {"running": {}}} for c in tpl["spec"]["containers"]],
            },
        })
    return pods


def _log(service, level, msg, **fields):
    return json.dumps({"ts": "2026-11-01T10:00:00.000Z", "level": level,
                       "service": service, "msg": msg, **fields})


def _event(reason, pod, message, type_="Warning", count=3):
    return {"type": type_, "reason": reason, "message": message, "count": count,
            "involvedObject": {"kind": "Pod", "name": pod},
            "lastTimestamp": "2026-11-01T10:01:00Z"}


def build_run(root: Path, key: str, seed: int = 0) -> Path:
    rng = random.Random(seed)
    fv = load_catalog()[key]
    docs = load_manifests()
    for cmd in fv.commands:
        apply_command(docs, cmd)

    pods = _pods(docs)
    pod = {p["metadata"]["labels"]["app"]: p for p in pods}
    target = (fv.config_path or "").split(":")[0].split("/")[-1]
    events: list[dict] = []
    logs: dict[str, list[str]] = {a: [] for a in pod}

    def lg_ok(n):
        for _ in range(n):
            logs["loadgen"].append(_log("loadgen", "INFO", "request ok", status=200,
                                        latency_ms=rng.randint(8, 40), error=None))

    def lg_bad(n, status=502, err=None, lat=(5, 30)):
        for _ in range(n):
            msg = f"request error: {err}" if err else "request failed: bad gateway"
            logs["loadgen"].append(_log("loadgen", "WARNING", msg, status=0 if err else status,
                                        latency_ms=rng.randint(*lat), error=err))

    def not_ready(app):
        pod[app]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]

    def crashloop(app, reason="Error", restarts=4):
        cs = pod[app]["status"]["containerStatuses"][0]
        cs["restartCount"] = restarts
        cs["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
        cs["lastState"] = {"terminated": {"reason": reason, "exitCode": 137 if reason == "OOMKilled" else 1}}
        not_ready(app)

    f = fv.fault
    if f == "F00":
        lg_ok(200)
    elif f == "F01" or f == "F08":
        up = "orders" if target == "orders" else "frontend"
        if up == "orders":
            for _ in range(20):
                logs["frontend"].append(_log("frontend", "ERROR",
                    "upstream orders request failed: ConnectionError: HTTPConnectionPool(host='orders', port=5000): "
                    "Max retries exceeded with url: /orders (Caused by NewConnectionError('Failed to establish a new "
                    "connection: [Errno 111] Connection refused'))"))
            lg_bad(200)
        else:
            lg_bad(200, err="ConnectionError")
    elif f == "F02":
        if target == "orders":
            for _ in range(30):
                logs["orders"].append(_log("orders", "WARNING",
                    "cache unavailable: Error -2 connecting to redis-cache:6379. Name or service not known."))
            lg_ok(200)
        else:
            for _ in range(30):
                logs["frontend"].append(_log("frontend", "ERROR",
                    "upstream orders request failed: ConnectionError: HTTPConnectionPool(host='order', port=5000): "
                    "Max retries exceeded with url: /orders (Caused by NameResolutionError(\"HTTPConnection(host='order', "
                    "port=5000): Failed to resolve 'order' ([Errno -2] Name or service not known)\"))"))
            lg_bad(200)
    elif f == "F03":
        for i in range(1, 6):
            logs["orders"].append(_log("orders", "ERROR",
                f"database connection failed (attempt {i}/5): connection to server at \"postgres\" (10.43.12.7), "
                "port 5432 failed: FATAL:  password authentication failed for user \"shop\""))
        logs["orders"].append(_log("orders", "CRITICAL", "giving up on database, exiting"))
        crashloop("orders")
        lg_bad(200)
    elif f == "F04":
        crashloop(target, reason="OOMKilled")
        events.append(_event("BackOff", pod[target]["metadata"]["name"], "Back-off restarting failed container"))
        lg_bad(200, err="ConnectionError" if target == "frontend" else None)
    elif f == "F05":
        not_ready(target)
        events.append(_event("Unhealthy", pod[target]["metadata"]["name"],
                             "Readiness probe failed: HTTP probe failed with statuscode: 404", count=20))
        path = "/readyz" if target == "orders" else "/healthz"
        logs[target].append(_log(target, "INFO", f'10.42.0.1 - - [01/Nov/2026 10:00:05] "GET {path} HTTP/1.1" 404 -'))
        lg_bad(200, err="ConnectionError" if target == "frontend" else None)
    elif f == "F06":
        pod["orders"]["status"]["containerStatuses"][0]["restartCount"] = 4
        events.append(_event("Unhealthy", pod["orders"]["metadata"]["name"],
                             'Liveness probe failed: Get "http://10.42.0.9:5001/health": dial tcp 10.42.0.9:5001: '
                             "connect: connection refused", count=12))
        events.append(_event("Killing", pod["orders"]["metadata"]["name"],
                             "Container orders failed liveness probe, will be restarted", type_="Normal"))
        lg_ok(120)
        lg_bad(80)
    elif f == "F07":
        cs = pod[target]["status"]["containerStatuses"][0]
        cs["state"] = {"waiting": {"reason": "ImagePullBackOff",
                                   "message": 'Back-off pulling image "demo-app:v2-missing"'}}
        not_ready(target)
        logs[target] = []
        events.append(_event("Failed", pod[target]["metadata"]["name"],
                             'Failed to pull image "demo-app:v2-missing": failed to resolve reference'))
        lg_bad(200, err="ConnectionError" if target == "frontend" else None)
    elif f == "F09":
        for _ in range(15):
            logs["orders"].append(_log("orders", "ERROR",
                'query failed: connection to server at "postgres" (10.43.12.7), port 5432 failed: timeout expired'))
        not_ready("orders")
        lg_bad(200, lat=(2000, 2100))
    elif f == "F10":
        events.append(_event("Unhealthy", pod[target]["metadata"]["name"],
                             'Liveness probe failed: Get "http://10.42.0.9:5000/health": context deadline exceeded '
                             "(Client.Timeout exceeded while awaiting headers)", count=6))
        pod[target]["status"]["containerStatuses"][0]["restartCount"] = 2
        lg_ok(60)
        lg_bad(100, err="ReadTimeout", lat=(5000, 5010))

    services = [d for d in docs if d["kind"] == "Service"]
    endpoints = []
    for s in services:
        sel = s["spec"].get("selector", {})
        sel_pods = [p for p in pods if all(p["metadata"]["labels"].get(k) == v for k, v in sel.items())]
        ready = [p for p in sel_pods if p["status"]["conditions"][0]["status"] == "True"]
        subsets = [{"addresses": [{"ip": "10.42.0.9"} for _ in ready],
                    "notReadyAddresses": [{"ip": "10.42.0.10"} for _ in sel_pods if _ not in ready]}] if sel_pods else []
        endpoints.append({"metadata": {"name": s["metadata"]["name"]}, "subsets": subsets})

    run_id = f"{key}-sim{seed:03d}"
    out = root / key / run_id
    (out / "logs").mkdir(parents=True)
    dump = lambda name, items: (out / name).write_text(json.dumps({"items": items}))  # noqa: E731
    dump("pods.json", pods)
    dump("services.json", services)
    dump("endpoints.json", endpoints)
    dump("deployments.json", [d for d in docs if d["kind"] == "Deployment"])
    dump("networkpolicies.json", [d for d in docs if d["kind"] == "NetworkPolicy"])
    dump("events.json", events)
    for app, lines in logs.items():
        name = pod[app]["metadata"]["name"]
        (out / "logs" / f"{name}__{app}.log").write_text("\n".join(lines) + "\n")
    (out / "meta.json").write_text(json.dumps({
        "run_id": run_id, "fault": fv.fault, "variant": fv.variant, "label": fv.fault,
        "config_path": fv.config_path, "inject_time": "2026-11-01T10:00:00Z", "simulated": True,
    }))
    return out


def resources_valid(docs) -> list[str]:
    def q(v):
        v = str(v)
        units = {"m": 1e-3, "Mi": 2**20, "Gi": 2**30, "Ki": 2**10}
        m = re.match(r"^([0-9.]+)(m|Mi|Gi|Ki)?$", v)
        return float(m.group(1)) * units.get(m.group(2), 1)
    bad = []
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            req, lim = c["resources"].get("requests", {}), c["resources"].get("limits", {})
            for r in req:
                if r in lim and q(req[r]) > q(lim[r]):
                    bad.append(f"{d['metadata']['name']}/{c['name']} {r}")
    return bad
