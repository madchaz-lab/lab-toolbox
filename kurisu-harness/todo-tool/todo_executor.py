#!/usr/bin/env python3
"""Todo executor (WS3) - the todo tool's VM-local probe component.

Probes are DATA, never shell. A closed allowlist of probe kinds, paths
confined to named roots (realpath-checked), URLs to named prefixes,
SQL to a single SELECT, CLI gates to three binaries with fixed
argument templates. Authenticated by a shared token header; every
call is audit-logged. See ~/pause-window/ws3-spec.md (threat model).

Endpoints:
  GET  /health
  POST /probe    {kind, ...}              -> {ok, kind, value, digest}
  POST /snapshot {label, probes: [...]}   -> {snapshot_id, results}
  GET  /snapshot/<id>                     -> stored snapshot
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = "/home/makise-kurisu"
TOKEN = open(os.path.join(HOME, ".secrets/todo-executor-token")).read().strip()
STATE_DIR = os.path.join(HOME, ".local/share/todo-executor")
SNAP_DIR = os.path.join(STATE_DIR, "snapshots")
LOG = os.path.join(STATE_DIR, "executor.log")
os.makedirs(SNAP_DIR, exist_ok=True)

ROOTS = [os.path.realpath(p) for p in (
    os.path.join(HOME, "projects"), os.path.join(HOME, "reports"),
    os.path.join(HOME, "pause-window"), os.path.join(HOME, "newsroom"),
    os.path.join(HOME, "chronoa-runner"), os.path.join(HOME, "skills"),
    os.path.join(HOME, "workspace"),
)]
ROOT_FILES = [os.path.join(HOME, "AGENTS.md"), os.path.join(HOME, ".config/opencode/opencode.json")]
URL_PREFIXES = ("http://127.0.0.1:", "http://192.168.27.205:",
                "http://192.168.33.206:", "http://192.168.33.215:")
MCP_SERVERS = {"hindsight": "http://127.0.0.1:8892/mcp/kurisu/",
               "hindsight-chronoa": "http://127.0.0.1:8891/mcp/chronoa-singlebank/"}
MCP_READ_TOOLS = {"recall"}
GATES = {"shellcheck": [os.path.join(HOME, ".local/bin/shellcheck")],
         "basedpyright": [os.path.join(HOME, ".local/bin/basedpyright")],
         "kubeconform": [os.path.join(HOME, ".local/bin/kubeconform")]}


def audit(kind, target, status):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {kind} {target} -> {status}\n"
    with open(LOG, "a") as f:
        f.write(line)


def confined(path):
    rp = os.path.realpath(path)
    if rp in ROOT_FILES:
        return rp
    for root in ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return rp
    raise ValueError(f"path outside allowed roots: {path}")


def digest_of(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def probe_file_hash(spec):
    p = confined(spec["path"])
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"path": p, "sha256": h.hexdigest(), "size": os.path.getsize(p)}


def probe_file_exists(spec):
    p = confined(spec["path"])
    return {"path": p, "exists": os.path.isfile(p),
            "size": os.path.getsize(p) if os.path.isfile(p) else None}


def probe_grep_count(spec):
    p = confined(spec["path"])
    pat = spec["pattern"]
    n = 0
    with open(p, errors="replace") as f:
        for line in f:
            if pat in line:
                n += 1
    return {"path": p, "pattern": pat, "count": n}


def probe_http_get(spec):
    url = spec["url"]
    if not url.startswith(URL_PREFIXES):
        raise ValueError(f"url not allowlisted: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "todo-executor"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(65536)
            return {"url": url, "status": r.status,
                    "body_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    except urllib.error.HTTPError as e:
        body = e.read(65536) if e.fp else b""
        return {"url": url, "status": e.code,
                "body_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def probe_sql_select(spec):
    sql = spec["sql"].strip()
    if not re.match(r"(?is)^select\b", sql) or ";" in sql.rstrip(";"):
        raise ValueError("sql-select takes exactly one SELECT statement")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|copy)\b", sql):
        raise ValueError("write keywords are refused in sql-select")
    pod = subprocess.run(
        ["kubectl", "-n", "brains-containment", "get", "pods", "-l", "app=kurisu-postgres",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    if not pod:
        raise ValueError("postgres pod not found")
    r = subprocess.run(
        ["kubectl", "-n", "brains-containment", "exec", pod, "--",
         "psql", "-U", "kurisu", "-d", "kurisu", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError(f"psql failed: {r.stderr.strip()[:200]}")
    rows = [ln for ln in r.stdout.splitlines() if ln.strip()][:100]
    return {"rows": rows, "row_count": len(rows)}


def probe_cli_gate(spec):
    gate = spec["gate"]
    if gate not in GATES:
        raise ValueError(f"unknown gate: {gate}")
    p = confined(spec["path"])
    argv = GATES[gate] + [p]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    tail = (r.stdout + r.stderr).strip().splitlines()[-20:]
    return {"gate": gate, "path": p, "exit_code": r.returncode, "tail": tail}


_hindsight_key = {"v": None, "at": 0}

def _tenant_key():
    if _hindsight_key["v"] and time.time() - _hindsight_key["at"] < 300:
        return _hindsight_key["v"]
    r = subprocess.run(
        ["kubectl", "-n", "brains-containment", "get", "secret", "hindsight-secrets",
         "-o", "jsonpath={.data.tenant-api-key}"],
        capture_output=True, text=True, timeout=30)
    import base64
    key = base64.b64decode(r.stdout.strip()).decode()
    _hindsight_key.update(v=key, at=time.time())
    return key


def probe_mcp_call(spec):
    server = spec["server"]
    tool = spec["tool"]
    if server not in MCP_SERVERS:
        raise ValueError(f"mcp server not allowlisted: {server}")
    if tool not in MCP_READ_TOOLS:
        raise ValueError(f"mcp tool not allowlisted for the executor: {tool}")
    url = MCP_SERVERS[server]
    headers = {"Authorization": "Bearer " + _tenant_key(),
               "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def rpc(payload, session=None):
        h = dict(headers)
        if session:
            h["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            sid = resp.headers.get("mcp-session-id")
            raw = resp.read().decode()
        data = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:])
        return data, sid

    init, sid = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "todo-executor", "version": "1"}}})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    out, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": tool, "arguments": spec.get("arguments") or {}}}, sid)
    if not out or "result" not in out:
        raise ValueError(f"mcp call failed: {str(out)[:200]}")
    text = ""
    for c in out["result"].get("content", []):
        if c.get("type") == "text":
            text += c["text"]
    return {"server": server, "tool": tool, "result_text": text[:4000]}


PROBES = {"file-hash": probe_file_hash, "file-exists": probe_file_exists,
          "grep-count": probe_grep_count, "http-get": probe_http_get,
          "sql-select": probe_sql_select, "cli-gate": probe_cli_gate,
          "mcp-call": probe_mcp_call}


def run_probe(spec):
    kind = spec.get("kind")
    if kind not in PROBES:
        raise ValueError(f"unknown probe kind: {kind}")
    value = PROBES[kind](spec)
    return {"ok": True, "kind": kind, "value": value, "digest": digest_of(value)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        got = self.headers.get("X-Executor-Token", "")
        return hmac.compare_digest(got, TOKEN)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "todo-executor"})
        if self.path.startswith("/snapshot/"):
            if not self._auth():
                return self._send(403, {"error": "bad token"})
            sid = self.path.split("/", 2)[2]
            fp = os.path.join(SNAP_DIR, sid + ".json")
            if not os.path.isfile(fp):
                return self._send(404, {"error": "no such snapshot"})
            return self._send(200, json.load(open(fp)))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            spec = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad body: {e}"})
        if self.path == "/probe":
            try:
                out = run_probe(spec)
                audit(spec.get("kind", "?"), str(spec.get("path") or spec.get("url") or spec.get("tool") or "")[:80], "ok")
                return self._send(200, out)
            except Exception as e:
                audit(spec.get("kind", "?"), "", f"refused: {str(e)[:80]}")
                return self._send(200, {"ok": False, "kind": spec.get("kind"), "error": str(e)[:300]})
        if self.path == "/snapshot":
            results = []
            for ps in spec.get("probes") or []:
                try:
                    results.append(run_probe(ps))
                except Exception as e:
                    results.append({"ok": False, "kind": ps.get("kind"), "error": str(e)[:300]})
            sid = uuid.uuid4().hex[:16]
            rec = {"snapshot_id": sid, "label": spec.get("label"), "at": int(time.time()), "results": results}
            with open(os.path.join(SNAP_DIR, sid + ".json"), "w") as f:
                json.dump(rec, f)
            audit("snapshot", spec.get("label", "")[:60], sid)
            return self._send(200, rec)
        return self._send(404, {"error": "not found"})


# --- verdict loop ---------------------------------------------------------
# The todo pod cannot reach this service (the VM's NAT forwards only
# declared ports), so the direction flips: the executor polls the board
# (Postgres, via kubectl - the VM's own tooling) for work orders in
# done_proposed whose completion carries a probe spec and no verdict
# for that completion, runs the probe, and records the verdict through
# todo.record_probe with an executor-class token (minted from the VM's
# orchestrator token file, the same trust domain as the reconciler).
def _psql(sql):
    pod = subprocess.run(
        ["kubectl", "-n", "brains-containment", "get", "pods", "-l", "app=kurisu-postgres",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    r = subprocess.run(
        ["kubectl", "-n", "brains-containment", "exec", pod, "--",
         "psql", "-U", "kurisu", "-d", "kurisu", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip()[:200])
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def verdict_loop():
    sys.path.insert(0, HOME)
    try:
        from class_crossing_recipe import Client, ORCH
    except Exception as e:
        audit("verdict-loop", "import", f"disabled: {e}")
        return
    orch = Client(ORCH)
    while True:
        try:
            rows = _psql(
                "SELECT id, metadata->'completion'->>'at', metadata->'completion'->'probe', "
                "COALESCE(metadata->'probe_result'->>'completion_at','') "
                "FROM todo_work_orders WHERE status='done_proposed' "
                "AND metadata->'completion'->'probe' IS NOT NULL")
            for row in rows:
                parts = row.split("|")
                if len(parts) < 4:
                    continue
                wo_id, comp_at, probe_json, verdict_at = parts[0], parts[1], parts[2], parts[3]
                if verdict_at == comp_at:
                    continue
                spec = json.loads(probe_json)
                spec = {k: v for k, v in spec.items() if k != "id"}
                try:
                    out = run_probe(spec)
                    value, digest = out["value"], out["digest"]
                except Exception as e:
                    value, digest = {"error": str(e)[:300]}, digest_of({"error": str(e)[:300]})
                tok = orch.call("todo.mint_token", {"token_cls": "executor", "binding": {}})
                ex = Client(tok["token"])
                r = ex.call("todo.record_probe", {
                    "work_order_id": wo_id,
                    "probe_id": json.loads(probe_json).get("id"),
                    "digest": digest, "value": value,
                    "completion_at": int(comp_at)})
                audit("verdict", wo_id[:8], f"recorded {str(r)[:60]}")
            rows = _psql(
                "SELECT id, metadata->'completion'->>'at', metadata->'completion'->'probe', "
                "COALESCE(metadata->'probe_result'->>'digest',''), "
                "COALESCE(metadata->'probe_confirm'->>'completion_at','') "
                "FROM todo_work_orders WHERE status='done_proposed' "
                "AND metadata->'completion'->'probe' IS NOT NULL "
                "AND metadata->>'accepted_by' IS NOT NULL "
                "AND metadata->'probe_result' IS NOT NULL")
            for row in rows:
                parts = row.split("|")
                if len(parts) < 5:
                    continue
                wo_id, comp_at, probe_json, vdigest, confirm_at = parts
                if confirm_at == comp_at:
                    continue
                spec = {k: v for k, v in json.loads(probe_json).items() if k != "id"}
                try:
                    out = run_probe(spec)
                    value, digest = out["value"], out["digest"]
                except Exception as e:
                    value, digest = {"error": str(e)[:300]}, digest_of({"error": str(e)[:300]})
                tok = orch.call("todo.mint_token", {"token_cls": "executor", "binding": {}})
                ex = Client(tok["token"])
                r = ex.call("todo.record_probe", {
                    "work_order_id": wo_id,
                    "probe_id": json.loads(probe_json).get("id"),
                    "digest": digest, "value": value,
                    "completion_at": int(comp_at), "phase": "confirm"})
                audit("confirm", wo_id[:8], f"recorded {str(r)[:60]}")
        except Exception as e:
            audit("verdict-loop", "poll", f"error: {str(e)[:120]}")
        time.sleep(30)


if __name__ == "__main__":
    servers = []
    for addr in ("0.0.0.0",):
        srv = ThreadingHTTPServer((addr, 8893), Handler)
        servers.append(srv)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    import threading as _th
    _th.Thread(target=verdict_loop, daemon=True).start()
    print("todo-executor on 0.0.0.0:8893", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
