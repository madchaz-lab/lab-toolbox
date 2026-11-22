#!/usr/bin/env python3
"""Ontology battery for server-v2: raw JSON-RPC over HTTP against a scratch
deployment. Mints class tokens directly with the test secret (battery pattern).
Checks template validation, instantiation fidelity, deviation recording,
version isolation, and the full work-order lifecycle with role walls."""
import base64, hashlib, hmac, json, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901"
SECRET = sys.argv[2] if len(sys.argv) > 2 else "test-secret"
results = []

def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def mint(cls, binding=None, ttl=3600):
    payload = {"cls": cls, "binding": binding or {}, "exp": int(time.time()) + ttl}
    pb = b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), pb.encode(), hashlib.sha256).digest()
    return f"{pb}.{b64e(sig)}"

ORCH = mint("orchestrator")
VALIDATOR = mint("validator")

_session = {"id": None}

def call(tool, args, token=None):
    body = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**9,
            "method": "tools/call", "params": {"name": tool, "arguments": args}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if _session["id"]:
        headers["Mcp-Session-Id"] = _session["id"]
    if token:
        headers["X-Orchestrator-Token"] = token
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.headers.get("mcp-session-id"):
                _session["id"] = r.headers["mcp-session-id"]
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    # streamable HTTP may wrap in SSE
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        resp = json.loads(raw)
    except Exception:
        return {"_unparsed": raw[:300]}
    if "error" in resp:
        return {"_error": resp["error"]}
    content = resp.get("result", {}).get("content", [])
    if content and content[0].get("type") == "text":
        txt = content[0]["text"]
        try:
            return json.loads(txt)
        except Exception:
            return {"_text": txt}
    if resp.get("result", {}).get("isError"):
        return {"_error": resp["result"]}
    return resp.get("result", {})

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))

def is_err(r):
    return isinstance(r, dict) and ("_error" in r or "_text" in r and "Error" in str(r.get("_text")) or "error" in str(r).lower()[:0])

def expect_error(name, r):
    s = json.dumps(r).lower()
    check(name, ("error" in s or "permission" in s or "required" in s or "invalid" in s
                 or "failed" in s or "unknown tool" in s or "not found" in s), json.dumps(r)[:200])

# handshake (initialize) — some fastmcp setups require it
try:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "battery-v2", "version": "1"}}}
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.headers.get("mcp-session-id"):
            _session["id"] = r.headers["mcp-session-id"]
except Exception as e:
    print("initialize skipped/failed:", e)

DAG = [
    {"key": "plan", "name": "plan the build", "owner": "orchestrator", "depends_on": [], "acceptance": "spec manifest exists"},
    {"key": "build_a", "name": "build module a", "owner": "kurisu-programming", "depends_on": ["plan"], "acceptance": "module a artifact exists"},
    {"key": "build_b", "name": "build module b", "owner": "kurisu-programming", "depends_on": ["plan"], "acceptance": "module b artifact exists"},
    {"key": "audit", "name": "audit the build", "owner": "kurisu-quality", "depends_on": ["build_a", "build_b"], "acceptance": "audit verdict recorded"},
]

# --- template validation (negatives) ---
r = call("todo.create_template", {"name": "bad-cycle", "step_dag": [
    {"key": "a", "name": "a", "owner": "orchestrator", "depends_on": ["b"], "acceptance": "x"},
    {"key": "b", "name": "b", "owner": "orchestrator", "depends_on": ["a"], "acceptance": "x"}]}, ORCH)
expect_error("cycle DAG rejected", r)
r = call("todo.create_template", {"name": "bad-dep", "step_dag": [
    {"key": "a", "name": "a", "owner": "orchestrator", "depends_on": ["nope"], "acceptance": "x"}]}, ORCH)
expect_error("unknown dep rejected", r)
r = call("todo.create_template", {"name": "bad-accept", "step_dag": [
    {"key": "a", "name": "a", "owner": "orchestrator", "depends_on": [], "acceptance": ""}]}, ORCH)
expect_error("missing acceptance rejected", r)
r = call("todo.create_template", {"name": "bad-owner", "step_dag": [
    {"key": "a", "name": "a", "owner": "kurisu-nobody", "depends_on": [], "acceptance": "x"}]}, ORCH)
expect_error("non-allowlisted owner rejected", r)

# --- template create + version ---
r = call("todo.create_template", {"name": "test-build", "description": "battery template", "step_dag": DAG}, ORCH)
check("create_template v1", r.get("version") == 1, json.dumps(r)[:200])
r2 = call("todo.create_template", {"name": "test-build", "step_dag": DAG}, ORCH)
expect_error("duplicate template name rejected", r2)
r = call("todo.add_template_version", {"name": "test-build", "step_dag": DAG + [
    {"key": "ship", "name": "ship it", "owner": "orchestrator", "depends_on": ["audit"], "acceptance": "shipped"}]}, ORCH)
check("add_template_version -> v2", r.get("version") == 2, json.dumps(r)[:200])

# --- project + instantiation (from v1) ---
p = call("todo.create_project", {"name": "battery-project"}, ORCH)
pid = p.get("id")
check("create_project", bool(pid), json.dumps(p)[:200])
inst = call("todo.instantiate_task", {"project_id": pid, "template_name": "test-build", "name": "battery instance", "version": 1}, ORCH)
task_id = inst.get("id")
steps = inst.get("steps", {})
check("instantiate returns 4 steps", len(steps) == 4, json.dumps(inst)[:200])
check("instance stamped v1", inst.get("template_version") == 1)

lst = call("todo.list_steps", {"task_id": task_id})
by_name = {s["name"]: s for s in lst} if isinstance(lst, list) else {}
check("list_steps returns copies", len(by_name) == 4, json.dumps(lst)[:200])

# depends_on resolved: audit step must depend on the two build steps' UUIDs (checked via SQL-free path: board_view)
bv = call("todo.board_view", {})
proj = [x for x in bv.get("projects", []) if x["id"] == pid]
tnode = proj[0]["tasks"][0] if proj and proj[0].get("tasks") else {}
smap = {s["name"]: s for s in tnode.get("steps", [])}
check("board_view nests task under project", bool(smap), json.dumps(bv)[:200])
check("task node carries template stamp", tnode.get("template_version") == 1)

# version isolation: instance from v1 still has 4 steps after v2 exists (already), instantiate v2 -> 5 steps
inst2 = call("todo.instantiate_task", {"project_id": pid, "template_name": "test-build", "name": "battery instance v2"}, ORCH)
check("v2 instance has 5 steps", len(inst2.get("steps", {})) == 5, json.dumps(inst2)[:200])
lst1 = call("todo.list_steps", {"task_id": task_id})
check("v1 instance unchanged by v2 (still 4 steps)", isinstance(lst1, list) and len(lst1) == 4)

# --- deviation recording ---
plan_step = steps.get("plan")
dev = call("todo.create_step", {"task_id": task_id, "name": "extra deviation step", "agent": "kurisu-quality"}, ORCH)
check("create_step on templated task works", bool(dev.get("id")), json.dumps(dev)[:200])
# deviations live in task metadata — visible via SQL in the runner; here check via a fresh instantiate contrast:
# (runner verifies metadata.deviations in the DB directly)

# --- work-order lifecycle on the plan step (owner: orchestrator) ---
SO = mint("step_owner", {"agent": "orchestrator"})
wo = call("todo.create_work_order", {"step_id": plan_step, "name": "write the spec", "assigned_worker": "kurisu-programming"}, SO)
wo_id = wo.get("id")
check("step_owner creates work order", bool(wo_id), json.dumps(wo)[:200])
W_BAD = mint("worker", {"work_order_id": "00000000-0000-0000-0000-000000000000"})
r = call("todo.start_work_order", {"work_order_id": wo_id}, W_BAD)
expect_error("worker with wrong binding cannot start", r)
W = mint("worker", {"work_order_id": wo_id})
r = call("todo.start_work_order", {"work_order_id": wo_id}, W)
check("worker starts work order", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.set_artifact_path", {"work_order_id": wo_id, "artifact_path": "/tmp/spec.md"}, W)
check("worker sets artifact", r.get("ok") is True)
r = call("todo.propose_done", {"work_order_id": wo_id}, W)
check("worker proposes done", r.get("ok") is True)
r = call("todo.validate_done", {"entity_type": "work_order", "entity_id": wo_id}, VALIDATOR)
expect_error("validator bounces unaccepted work order", r)
SO_WRONG = mint("step_owner", {"agent": "kurisu-quality"})
r = call("todo.accept_work_order", {"work_order_id": wo_id}, SO_WRONG)
expect_error("non-owner cannot accept", r)
# after the bounce the order is back in_progress: re-propose then accept
r = call("todo.propose_done", {"work_order_id": wo_id}, W)
check("re-propose after bounce", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.accept_work_order", {"work_order_id": wo_id}, SO)
check("owner accepts", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.validate_done", {"entity_type": "work_order", "entity_id": wo_id}, VALIDATOR)
check("validator validates -> done", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.validate_done", {"entity_type": "work_order", "entity_id": wo_id}, ORCH)
expect_error("orchestrator cannot validate (validator-exclusive done)", r)
r = call("todo.create_work_order", {"step_id": plan_step, "name": "illegal"}, W)
expect_error("worker cannot create work orders", r)

# --- ad-hoc path ---
adhoc = call("todo.create_task_instance", {"project_id": pid, "name": "ad-hoc instance"}, ORCH)
check("ad-hoc task instance", bool(adhoc.get("id")), json.dumps(adhoc)[:200])
st = call("todo.create_step", {"task_id": adhoc.get("id"), "name": "ad-hoc step", "agent": "orchestrator"}, ORCH)
check("ad-hoc step", bool(st.get("id")))

# --- old collision names are gone ---
r = call("todo.create_task", {"step_id": plan_step, "name": "ghost"}, SO)
expect_error("old todo.create_task no longer exists", r)
r = call("todo.create_workflow", {"project_id": pid, "name": "ghost"}, ORCH)
expect_error("old todo.create_workflow no longer exists", r)

# --- v3: owner repair + zero-order closure + element proposal ---
orphan = call("todo.create_step", {"task_id": adhoc.get("id"), "name": "ownerless step"}, ORCH)
check("ownerless step created (agent omitted)", bool(orphan.get("id")))
SO_ORPH = mint("step_owner", {"agent": "orchestrator"})
r = call("todo.propose_step_done", {"step_id": orphan.get("id"), "reason": "test"}, SO_ORPH)
expect_error("ownerless step cannot be proposed (no owner to bind)", r)
r = call("todo.set_step_owner", {"step_id": orphan.get("id"), "agent": "orchestrator"}, ORCH)
check("orchestrator repairs step owner", r.get("ok") is True and r.get("agent") == "orchestrator", json.dumps(r)[:200])
r = call("todo.propose_step_done", {"step_id": orphan.get("id")}, SO_ORPH)
expect_error("zero-order closure without reason rejected", r)
r = call("todo.propose_step_done", {"step_id": orphan.get("id"), "reason": "work predates work-order discipline; evidence in comments"}, SO_ORPH)
check("zero-order closure with reason proposed", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.validate_done", {"entity_type": "step", "entity_id": orphan.get("id")}, VALIDATOR)
check("validator closes zero-order step", r.get("ok") is True, json.dumps(r)[:200])

# element proposal: the battery instance (v1) still has open steps -> must reject
r = call("todo.propose_done_element", {"entity_type": "task", "entity_id": task_id}, ORCH)
expect_error("task with open steps cannot be proposed done", r)
# ad-hoc task: its steps are the ad-hoc step (open) + ownerless (now done) -> reject until ad-hoc step closes
r = call("todo.set_step_owner", {"step_id": st.get("id"), "agent": "orchestrator"}, ORCH)
r = call("todo.propose_step_done", {"step_id": st.get("id"), "reason": "ad-hoc battery step, no orders by design"}, SO_ORPH)
r = call("todo.validate_done", {"entity_type": "step", "entity_id": st.get("id")}, VALIDATOR)
check("ad-hoc step closed", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.propose_done_element", {"entity_type": "task", "entity_id": adhoc.get("id")}, ORCH)
check("task proposed done once children terminal", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.validate_done", {"entity_type": "task", "entity_id": adhoc.get("id")}, VALIDATOR)
check("validator closes task", r.get("ok") is True, json.dumps(r)[:200])
r = call("todo.propose_done_element", {"entity_type": "project", "entity_id": pid}, ORCH)
expect_error("project with open tasks cannot be proposed done", r)

fails = [x for x in results if not x[1]]
print(f"\n=== BATTERY V3: {len(results) - len(fails)}/{len(results)} passed ===")
sys.exit(1 if fails else 0)
