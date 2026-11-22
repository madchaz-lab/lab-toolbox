#!/usr/bin/env python3
"""
Filtering proxy for Hindsight MCP (Kurisu's general instance).

Implements the filtering-proxy spec (project filtering-proxy 50edf8dd,
spec_manifest.md): sits in front of the Hindsight MCP endpoint and
- removes exactly three tools from tools/list responses:
  clear_memories, delete_bank, clear_mental_model;
- refuses calls to those three with a deterministic JSON-RPC error
  (-32000), audit-logged;
- enforces a read-only path (batch 10, 2026-11-22): on
  /mcp/chronoa-singlebank/ only read verbs are forwarded; every
  write call is refused the same way - Kurisu's access to that bank
  is recall/reflect by dispatch, retain denied, and the denial is
  mechanical now that the bank has an MCP entry of its own;
- passes everything else through unchanged (arguments, results,
  headers, session ids, SSE framing).

Unlike the Chronoa curated instance (single fixed bank), this proxy
preserves the request path: any /mcp/<bank>/ path is forwarded to the
same path on the upstream, so one instance serves every bank.
Non-/mcp/ paths are refused (404) - the proxy is not a general
forwarder.

Built by Muse during the 2026-11-21 pause window, at madchaz's
direction, from the verified Chronoa curated proxy
(~/newsroom/chronoa-proxy/chronoa_curated_proxy.py). Stdlib only.
The proxy holds no credentials: the client's Authorization header is
forwarded to the upstream as-is.

Env:
  FILTER_UPSTREAM_HOST  upstream base (default http://192.168.33.215:8888)
  FILTER_PORT           listen port on 127.0.0.1 (default 8892)
  FILTER_LOG            audit log path (default ~/projects/filtering-proxy/proxy.log)
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("FILTER_UPSTREAM_HOST", "http://192.168.33.215:8888")
PORT = int(os.environ.get("FILTER_PORT", "8892"))
LOG_PATH = os.environ.get(
    "FILTER_LOG",
    os.path.expanduser("~/projects/filtering-proxy/proxy.log"),
)
FORBIDDEN = {"clear_memories", "delete_bank", "clear_mental_model"}

# Batch 10 (2026-11-22): paths that forward read verbs only. Kurisu's
# access to chronoa-singlebank is recall/reflect by dispatch with
# retain denied (AGENTS.md section 7); now that the bank has its own
# MCP entry in her config, the denial is enforced here, mechanically,
# instead of resting on the caller's discipline.
READ_VERBS = {
    "recall", "reflect",
    "list_memories", "get_memory",
    "list_documents", "get_document",
    "list_mental_models", "get_mental_model",
    "list_directives",
    "get_bank", "list_tags",
    "get_page_tree", "search_pages", "get_page",
    "list_operations", "get_operation",
}
READONLY_PATHS = {"/mcp/chronoa-singlebank/"}

_log_lock = threading.Lock()


def audit(event, details):
    line = json.dumps({"event": event, **details})
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line, file=sys.stderr, flush=True)


def filter_tools_payload(obj):
    """If obj is a JSON-RPC result carrying a tools list, strip the
    forbidden tools. Returns (obj, removed_names)."""
    removed = []
    if isinstance(obj, dict):
        result = obj.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            kept = []
            for tool in result["tools"]:
                name = tool.get("name") if isinstance(tool, dict) else None
                if name in FORBIDDEN:
                    removed.append(name)
                else:
                    kept.append(tool)
            if removed:
                result["tools"] = kept
    return obj, removed


def filter_body(body, content_type):
    """Filter a tools/list result out of a JSON or SSE body."""
    if "text/event-stream" in (content_type or ""):
        out_lines = []
        removed_all = []
        for line in body.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    obj = json.loads(payload)
                except (ValueError, TypeError):
                    out_lines.append(line)
                    continue
                obj, removed = filter_tools_payload(obj)
                removed_all.extend(removed)
                out_lines.append("data: " + json.dumps(obj) if removed else line)
            else:
                out_lines.append(line)
        return "\n".join(out_lines), removed_all
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return body, []
    obj, removed = filter_tools_payload(obj)
    if removed:
        return json.dumps(obj), removed
    return body, []


def forbidden_call(req_obj):
    """Return (tool_name, req_id) if req_obj (single or batch) contains
    a tools/call to a forbidden tool, else None."""
    items = req_obj if isinstance(req_obj, list) else [req_obj]
    for item in items:
        if isinstance(item, dict) and item.get("method") == "tools/call":
            name = (item.get("params") or {}).get("name")
            if name in FORBIDDEN:
                return name, item.get("id")
    return None


def readonly_violation(req_obj, path):
    """On a read-only path, return (tool_name, req_id) for the first
    tools/call naming a verb outside READ_VERBS, else None."""
    if path not in READONLY_PATHS:
        return None
    items = req_obj if isinstance(req_obj, list) else [req_obj]
    for item in items:
        if isinstance(item, dict) and item.get("method") == "tools/call":
            name = (item.get("params") or {}).get("name")
            if name is not None and name not in READ_VERBS:
                return name, item.get("id")
    return None


def wants_tools_list(req_obj):
    items = req_obj if isinstance(req_obj, list) else [req_obj]
    return any(isinstance(i, dict) and i.get("method") == "tools/list" for i in items)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body_bytes, content_type, session_id=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if session_id:
            self.send_header("mcp-session-id", session_id)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def _refuse(self, tool_name, req_id):
        audit("call_refused", {"tool": tool_name, "path": self.path})
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": f"Tool '{tool_name}' is forbidden by filtering proxy",
            },
            "id": req_id,
        }).encode()
        self._send(200, body, "application/json")

    def _refuse_readonly(self, tool_name, req_id):
        audit("call_refused_readonly", {"tool": tool_name, "path": self.path})
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": f"Tool '{tool_name}' is refused on the read-only path {self.path} by filtering proxy",
            },
            "id": req_id,
        }).encode()
        self._send(200, body, "application/json")

    def _forward(self, method):
        if not self.path.startswith("/mcp/"):
            self._send(404, b'{"error":"only /mcp/ paths are proxied"}', "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        req_obj = None
        if raw:
            try:
                req_obj = json.loads(raw)
            except (ValueError, TypeError):
                req_obj = None
        if req_obj is not None:
            hit = forbidden_call(req_obj)
            if hit:
                self._refuse(hit[0], hit[1])
                return
            ro = readonly_violation(req_obj, self.path)
            if ro:
                self._refuse_readonly(ro[0], ro[1])
                return
        url = UPSTREAM_HOST.rstrip("/") + self.path
        req = urllib.request.Request(url, data=raw if method == "POST" else None, method=method)
        for h in ("Content-Type", "Accept", "Authorization", "Mcp-Session-Id"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            status = resp.status
            ctype = resp.headers.get("Content-Type")
            sid = resp.headers.get("mcp-session-id")
            body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            ctype = e.headers.get("Content-Type") if e.headers else None
            sid = e.headers.get("mcp-session-id") if e.headers else None
            body = e.read().decode("utf-8", "replace")
        except Exception as e:  # upstream unreachable
            audit("upstream_error", {"error": str(e), "path": self.path})
            body = json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": f"filtering proxy upstream error: {e}"},
                "id": req_obj.get("id") if isinstance(req_obj, dict) else None,
            })
            self._send(502, body.encode(), "application/json")
            return
        if req_obj is not None and wants_tools_list(req_obj):
            body, removed = filter_body(body, ctype)
            if removed:
                audit("catalog_filtered", {"removed": removed, "path": self.path})
        self._send(status, body.encode(), ctype, sid)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def do_DELETE(self):
        self._forward("DELETE")


def main():
    audit("proxy_start", {"upstream_host": UPSTREAM_HOST, "port": PORT, "forbidden": sorted(FORBIDDEN), "readonly_paths": sorted(READONLY_PATHS)})
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
