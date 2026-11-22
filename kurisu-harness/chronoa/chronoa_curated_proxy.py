#!/usr/bin/env python3
"""
Curated Hindsight MCP proxy for Chronoa (chronoa-singlebank).

Sits in front of the real Hindsight MCP endpoint and enforces the
filtering-proxy spec (Kurisu project filtering-proxy 50edf8dd):
- tools/list responses have exactly three tools removed:
  clear_memories, delete_bank, clear_mental_model.
- Calls to those three are refused with a deterministic JSON-RPC
  error and audit-logged.
- Everything else passes through unchanged (headers, session ids,
  SSE framing preserved; tools/list payloads inside SSE are
  filtered in place).

Stdlib only. The proxy holds no credentials: the client's
Authorization header is forwarded to the upstream as-is.

Env:
  CURATED_UPSTREAM  upstream MCP URL (default: chronoa-singlebank)
  CURATED_PORT      listen port on 127.0.0.1 (default 8891)
  CURATED_LOG       audit log path (default ~/newsroom/chronoa-proxy/proxy.log)
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get(
    "CURATED_UPSTREAM",
    "http://192.168.33.215:8888/mcp/chronoa-singlebank/",
)
PORT = int(os.environ.get("CURATED_PORT", "8891"))
LOG_PATH = os.environ.get(
    "CURATED_LOG",
    os.path.expanduser("~/newsroom/chronoa-proxy/proxy.log"),
)
FORBIDDEN = {"clear_memories", "delete_bank", "clear_mental_model"}

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
        audit("call_refused", {"tool": tool_name})
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": f"Tool '{tool_name}' is forbidden by filtering proxy",
            },
            "id": req_id,
        }).encode()
        self._send(200, body, "application/json")

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        req_obj = None
        if raw:
            try:
                req_obj = json.loads(raw)
            except (ValueError, TypeError):
                req_obj = None
        if isinstance(req_obj, dict) and req_obj.get("method") == "tools/call":
            tool_name = (req_obj.get("params") or {}).get("name")
            if tool_name in FORBIDDEN:
                self._refuse(tool_name, req_obj.get("id"))
                return
        url = UPSTREAM
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
            audit("upstream_error", {"error": str(e)})
            body = json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": f"curated proxy upstream error: {e}"},
                "id": req_obj.get("id") if isinstance(req_obj, dict) else None,
            })
            self._send(502, body.encode(), "application/json")
            return
        if isinstance(req_obj, dict) and req_obj.get("method") == "tools/list":
            body, removed = filter_body(body, ctype)
            if removed:
                audit("catalog_filtered", {"removed": removed})
        self._send(status, body.encode(), ctype, sid)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def do_DELETE(self):
        self._forward("DELETE")


def main():
    audit("proxy_start", {"upstream": UPSTREAM, "port": PORT, "forbidden": sorted(FORBIDDEN)})
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
