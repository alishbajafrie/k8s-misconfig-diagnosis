"""Rule-based diagnosis: runtime symptom -> config field.

Rules are checked in order and the first match wins. Root-cause rules come
before probe rules because crashing pods also fail their probes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .features import symptom_features
from .parse import LogLine, Run


@dataclass
class Diagnosis:
    fault: str
    config_path: str | None
    rule: str
    explanation: str
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def _deployment(run: Run, name: str) -> dict | None:
    return next((d for d in run.deployments if d["metadata"]["name"] == name), None)


def _containers(dep: dict) -> list[dict]:
    return dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])


def _pod_app(run: Run, pod_name: str) -> str | None:
    for p in run.pods:
        if p["metadata"]["name"] == pod_name:
            return p["metadata"].get("labels", {}).get("app")
    m = re.match(r"^([a-z0-9-]+?)-[a-z0-9]{8,10}-[a-z0-9]{5}$", pod_name)
    return m.group(1) if m else None


def _first_container(run: Run, app: str) -> str:
    dep = _deployment(run, app)
    cs = _containers(dep) if dep else []
    return cs[0]["name"] if cs else app


def _selected_pods(run: Run, svc: dict) -> list[dict]:
    sel = svc.get("spec", {}).get("selector") or {}
    if not sel:
        return []
    return [p for p in run.pods
            if all(p["metadata"].get("labels", {}).get(k) == v for k, v in sel.items())]


def _app_services(run: Run) -> list[dict]:
    return [s for s in run.services if s["metadata"]["name"] != "kubernetes"]


def _cpu_quantity(q: str | None) -> float | None:
    if not q:
        return None
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def _log_matches(run: Run, pattern: re.Pattern) -> list[tuple[LogLine, re.Match]]:
    out = []
    for ln in run.logs:
        m = pattern.search(ln.msg)
        if m:
            out.append((ln, m))
    return out


def _fmt(ln: LogLine) -> str:
    tag = " (previous container)" if ln.previous else ""
    return f"[{ln.app}{tag}] {ln.level}: {ln.msg[:220]}"


def _unhealthy(run: Run, probe: str) -> list[dict]:
    return [e for e in run.events
            if e.get("reason") == "Unhealthy" and e.get("message", "").startswith(probe)]


# ---------------------------------------------------------------- rules

def rule_image(run: Run) -> Diagnosis | None:
    for p in run.pods:
        for cs in p.get("status", {}).get("containerStatuses", []) or []:
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason") in {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}:
                app = p["metadata"].get("labels", {}).get("app", "?")
                return Diagnosis(
                    "F07", f"deployment/{app}:containers[{cs['name']}].image", "image",
                    f"Container '{cs['name']}' cannot pull image '{cs.get('image')}'.",
                    [f"{w.get('reason')}: {w.get('message', '')[:200]}"],
                )
    return None


def rule_oom(run: Run) -> Diagnosis | None:
    for p in run.pods:
        for cs in p.get("status", {}).get("containerStatuses", []) or []:
            for st in ("state", "lastState"):
                t = (cs.get(st) or {}).get("terminated") or {}
                if t.get("reason") == "OOMKilled":
                    app = p["metadata"].get("labels", {}).get("app", "?")
                    return Diagnosis(
                        "F04", f"deployment/{app}:containers[{cs['name']}].resources.limits.memory",
                        "oom",
                        f"Container '{cs['name']}' was OOMKilled "
                        f"({cs.get('restartCount', 0)} restarts); its memory limit is too low.",
                        [f"{st}.terminated.reason=OOMKilled exitCode={t.get('exitCode')}"],
                    )
    return None


AUTH = re.compile(r"password authentication failed|authentication failed for user", re.I)


def rule_auth(run: Run) -> Diagnosis | None:
    hits = _log_matches(run, AUTH)
    if not hits:
        return None
    app = hits[0][0].app
    path = None
    dep = _deployment(run, app)
    for c in _containers(dep) if dep else []:
        for env in c.get("env", []) or []:
            ref = (env.get("valueFrom") or {}).get("secretKeyRef")
            if ref and "PASS" in env["name"].upper():
                path = f"secret/{ref['name']}:data.{ref['key']}"
    return Diagnosis("F03", path, "auth",
                     f"'{app}' is rejected by the database: the password it is given is wrong.",
                     [_fmt(ln) for ln, _ in hits[:3]])


DNS = re.compile(
    r'could not translate host name "(?P<h1>[^"]+)"'
    r"|Failed to resolve '(?P<h2>[^']+)'"
    r"|connecting to (?P<h3>[A-Za-z0-9.-]+):\d+\. (?:Name or service not known|Temporary failure in name resolution|No address associated with hostname)"
)


def _env_holding_host(dep: dict, host: str) -> tuple[str, str] | None:
    for c in _containers(dep):
        for env in c.get("env", []) or []:
            val = env.get("value")
            if val is None:
                continue
            if val == host or urlparse(val).hostname == host:
                return c["name"], env["name"]
    return None


def rule_dns(run: Run) -> Diagnosis | None:
    service_names = {s["metadata"]["name"] for s in run.services}
    for ln, m in _log_matches(run, DNS):
        host = m.group("h1") or m.group("h2") or m.group("h3")
        if host in service_names:
            continue  # existing service, not a typo
        dep = _deployment(run, ln.app)
        found = _env_holding_host(dep, host) if dep else None
        path = f"deployment/{ln.app}:containers[{found[0]}].env.{found[1]}" if found else None
        var = found[1] if found else "an environment variable"
        return Diagnosis("F02", path, "dns",
                         f"'{ln.app}' tries to reach host '{host}', which is not a Service "
                         f"in the namespace. {var} holds the wrong hostname.",
                         [_fmt(ln)])
    return None


def rule_selector(run: Run) -> Diagnosis | None:
    for svc in _app_services(run):
        if svc.get("spec", {}).get("selector") and not _selected_pods(run, svc):
            name = svc["metadata"]["name"]
            return Diagnosis("F08", f"service/{name}:spec.selector", "selector",
                             f"Service '{name}' selector {svc['spec']['selector']} matches no pods, "
                             f"so it has no endpoints.",
                             [f"service/{name} selects 0 pods"])
    return None


def rule_target_port(run: Run) -> Diagnosis | None:
    for svc in _app_services(run):
        pods = _selected_pods(run, svc)
        if not pods:
            continue
        container_ports = {
            port.get("containerPort")
            for p in pods for c in p.get("spec", {}).get("containers", [])
            for port in c.get("ports", []) or []
        }
        port_names = {
            port.get("name")
            for p in pods for c in p.get("spec", {}).get("containers", [])
            for port in c.get("ports", []) or [] if port.get("name")
        }
        for i, sp in enumerate(svc["spec"].get("ports", [])):
            tp = sp.get("targetPort", sp.get("port"))
            if (isinstance(tp, int) and tp not in container_ports) or \
               (isinstance(tp, str) and tp not in port_names):
                name = svc["metadata"]["name"]
                return Diagnosis("F01", f"service/{name}:spec.ports[{i}].targetPort", "target_port",
                                 f"Service '{name}' forwards to port {tp}, but its pods listen on "
                                 f"{sorted(p for p in container_ports if p)}.",
                                 [f"service/{name} targetPort={tp}"])
    return None


# v0.2: a blocking NetworkPolicy can drop (timeout) or reject (refused),
# depending on the CNI. On k3s it mostly rejects. So match both, and tell it
# apart from a stopped server by checking that the target still has ready
# endpoints.
CONN_FAIL = re.compile(
    r'(?:connection to server at "(?P<h1>[^"]+)"'
    r"|connecting to (?P<h2>[A-Za-z0-9.-]+):\d+"
    r"|host='(?P<h3>[^']+)')"
    r".*?(?:Connection refused|timeout expired|timed out)",
    re.I | re.S,
)


def _labels_match(labels: dict, sel: dict) -> bool:
    return all(labels.get(k) == v for k, v in sel.items())


def _ready_addresses(run: Run, svc_name: str) -> int:
    for ep in run.endpoints:
        if ep["metadata"]["name"] == svc_name:
            return sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])
    return 0


def _ingress_allows(np_: dict, client_labels: dict) -> bool:
    # only podSelector peers are handled; that is all the demo app uses
    for rule in np_.get("spec", {}).get("ingress") or []:
        peers = rule.get("from")
        if not peers:
            return True
        for peer in peers:
            sel = (peer.get("podSelector") or {}).get("matchLabels")
            if sel is not None and _labels_match(client_labels, sel):
                return True
    return False


def rule_netpol(run: Run) -> Diagnosis | None:
    if not run.netpols:
        return None
    services = {s["metadata"]["name"]: s for s in _app_services(run)}
    for ln, m in _log_matches(run, CONN_FAIL):
        host = m.group("h1") or m.group("h2") or m.group("h3")
        svc = services.get(host)
        if not svc or _ready_addresses(run, host) == 0:
            continue  # unknown host, or the server is really down
        targets = _selected_pods(run, svc)
        client = next((p for p in run.pods if p["metadata"]["name"] == ln.pod), None)
        client_labels = client["metadata"].get("labels", {}) if client else {"app": ln.app}
        for np_ in run.netpols:
            if "Ingress" not in (np_.get("spec", {}).get("policyTypes") or ["Ingress"]):
                continue
            sel = np_.get("spec", {}).get("podSelector", {}).get("matchLabels", {}) or {}
            if not any(_labels_match(p["metadata"].get("labels", {}), sel) for p in targets):
                continue
            if _ingress_allows(np_, client_labels):
                continue
            name = np_["metadata"]["name"]
            return Diagnosis("F09", f"networkpolicy/{name}", "netpol",
                             f"'{ln.app}' cannot connect to Service '{host}' although it has "
                             f"{_ready_addresses(run, host)} ready endpoint(s), and NetworkPolicy "
                             f"'{name}' blocks ingress from '{ln.app}' to those pods.",
                             [_fmt(ln)])
    return None


# v0.2, F11. Written from F11-a dev runs only. A wrong port in a client
# setting: the host is a real Service, but the port is not one of its ports.
# On k3s a connection to a port the Service does not expose times out (it is
# dropped, not rejected). redis-py does not put host:port in the message
# ("Timeout connecting to server"), so the app logs them as fields too.
PORT_FAIL = re.compile(r"refused|timed out|timeout", re.I)
HOST_PORT = re.compile(
    r"host='(?P<h1>[^']+)', port=(?P<p1>\d+)"
    r"|connecting to (?P<h2>[A-Za-z0-9.-]+):(?P<p2>\d+)"
    r'|connection to server at "(?P<h3>[^"]+)" \([^)]*\), port (?P<p3>\d+)'
)


def _host_port(ln: LogLine) -> tuple[str, int] | None:
    m = HOST_PORT.search(ln.msg)
    if m:
        for i in "123":
            if m.group("h" + i):
                return m.group("h" + i), int(m.group("p" + i))
    for k, v in ln.fields.items():
        if k.endswith("_host") and str(ln.fields.get(k[:-5] + "_port", "")).isdigit():
            return str(v), int(ln.fields[k[:-5] + "_port"])
    return None


def _env_holding_port(dep: dict, host: str, port: int) -> tuple[str, str] | None:
    for c in _containers(dep):
        for env in c.get("env", []) or []:
            val = env.get("value")
            if val is None:
                continue
            if val == str(port):
                return c["name"], env["name"]
            u = urlparse(val)
            if u.hostname == host and u.port == port:
                return c["name"], env["name"]
    return None


def rule_client_port(run: Run) -> Diagnosis | None:
    services = {s["metadata"]["name"]: s for s in _app_services(run)}
    for ln, _ in _log_matches(run, PORT_FAIL):
        hp = _host_port(ln)
        if not hp:
            continue
        host, port = hp
        svc = services.get(host)
        if not svc or _ready_addresses(run, host) == 0:
            continue
        svc_ports = {sp.get("port") for sp in svc["spec"].get("ports", [])}
        if port in svc_ports:
            continue
        dep = _deployment(run, ln.app)
        found = _env_holding_port(dep, host, port) if dep else None
        path = f"deployment/{ln.app}:containers[{found[0]}].env.{found[1]}" if found else None
        var = found[1] if found else "a client setting"
        return Diagnosis("F11", path, "client_port",
                         f"'{ln.app}' connects to Service '{host}' on port {port}, but the Service "
                         f"only exposes {sorted(svc_ports)}. {var} holds the wrong port.",
                         [_fmt(ln)])
    return None


def rule_cpu(run: Run) -> Diagnosis | None:
    sym = symptom_features(run)
    slow = (sym["lg_p95_ms"] > 1000 or sym["lg_timeout_rate"] > 0.05
            or any("deadline exceeded" in e.get("message", "") or "Timeout" in e.get("message", "")
                   for e in _unhealthy(run, "Liveness") + _unhealthy(run, "Readiness")))
    if not slow:
        return None
    for dep in run.deployments:
        for c in _containers(dep):
            cpu = _cpu_quantity((c.get("resources", {}).get("limits") or {}).get("cpu"))
            if cpu is not None and cpu < 0.05:
                app = dep["metadata"]["name"]
                return Diagnosis("F10", f"deployment/{app}:containers[{c['name']}].resources.limits.cpu",
                                 "cpu",
                                 f"Requests are slow/timing out and '{app}' has a CPU limit of "
                                 f"{int(cpu * 1000)}m, so it is being throttled.",
                                 [f"loadgen p95={sym['lg_p95_ms']:.0f}ms timeout_rate={sym['lg_timeout_rate']:.2f}"])
    return None


def _probe_rule(run: Run, probe: str, fault: str, field_name: str) -> Diagnosis | None:
    evs = [e for e in _unhealthy(run, probe)
           if "connection refused" in e.get("message", "") or "statuscode" in e.get("message", "")]
    if not evs:
        return None
    pod = evs[0].get("involvedObject", {}).get("name", "")
    app = _pod_app(run, pod) or "?"
    c = _first_container(run, app)
    return Diagnosis(fault, f"deployment/{app}:containers[{c}].{field_name}", field_name,
                     f"{probe} probe of '{app}' keeps failing although the app itself logs no "
                     f"error: the probe settings are wrong.",
                     [e.get("message", "")[:220] for e in evs[:2]])


def rule_liveness(run: Run) -> Diagnosis | None:
    return _probe_rule(run, "Liveness", "F06", "livenessProbe")


def rule_readiness(run: Run) -> Diagnosis | None:
    return _probe_rule(run, "Readiness", "F05", "readinessProbe")


RULES = [rule_image, rule_oom, rule_auth, rule_dns, rule_selector, rule_target_port,
         rule_netpol, rule_client_port, rule_cpu, rule_liveness, rule_readiness]


def diagnose(run: Run) -> Diagnosis:
    for rule in RULES:
        d = rule(run)
        if d:
            return d
    sym = symptom_features(run)
    restarts = sum(cs.get("restartCount", 0) for p in run.pods
                   for cs in p.get("status", {}).get("containerStatuses", []) or [])
    if sym["lg_error_rate"] > 0.2 or restarts > 0:
        return Diagnosis("UNKNOWN", None, "none",
                         "Something is wrong (errors or restarts) but no rule explains it.",
                         [f"loadgen error_rate={sym['lg_error_rate']:.2f}, restarts={restarts}"])
    return Diagnosis("F00", None, "healthy", "No fault detected.", [])
