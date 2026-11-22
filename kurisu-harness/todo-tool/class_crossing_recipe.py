import json, urllib.request

BASE = "http://kurisu-todo.brains-containment.svc.cluster.local:8001/mcp"
ORCH = open("/home/makise-kurisu/.secrets/kurisu-todo-orchestrator-token").read().strip()

class Client:
    def __init__(self, token):
        self.token = token
        self.sid = None
    def _post(self, payload):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "X-Orchestrator-Token": self.token}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=60) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            return r.read().decode()
    def call(self, tool, args):
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "class-crossing", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        raw = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
        for line in raw.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                if d.get("error"):
                    return {"error": d["error"]}
                c = d.get("result", {}).get("content", [])
                if c and c[0].get("type") == "text":
                    t = c[0]["text"]
                    try:
                        return json.loads(t)
                    except Exception:
                        return t
        return {"error": "no result"}

orch = Client(ORCH)

# Import-safety (fixed 2026-11-19 by Muse, at madchaz's direction): this module
# previously ended with the one-off script that created the breach-record work
# order on 2026-11-18, left executable at module level - every import re-ran it
# and created another duplicate work order on the gates-record step. Importing
# this module now performs no network calls and creates nothing. Run it
# directly for a read-only smoke check of the token chain (mints a token and
# lists projects; writes nothing).
if __name__ == "__main__":
    minted = orch.call("todo.mint_token", {"token_cls": "step_owner", "binding": {"agent": "orchestrator"}})
    print("mint:", "ok" if isinstance(minted, dict) and minted.get("token") else minted)
    projects = orch.call("todo.list_projects", {})
    print("list_projects:", "ok" if isinstance(projects, (list, dict)) else projects)
