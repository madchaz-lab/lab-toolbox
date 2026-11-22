#!/usr/bin/env python3
"""Slot grant reconciler (WS2) - VM-local, runs on a 60s systemd timer.

Keeps the todo tool's slot ledger honest against the opencode session
store, which the in-cluster todo pod cannot read:

- releases a bound grant when its session has finished
  (idle_outcome set) or has been quiet for 30 minutes (paused or
  stalled - a resume re-requests its slot under AGENTS.md 4.2b);
- releases held grants past their TTL;
- logs an ANOMALY line for any actively-generating child session
  whose node has no bound grant naming it (cap-breach evidence;
  the reconciler never kills or pauses anything).

Activity is judged by session_message recency and the session row's
time_updated - NEVER by time_suspended, which is stamped at creation
for suspended-spawned workers and does not clear on resume.
"""
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/home/makise-kurisu")
from class_crossing_recipe import Client, ORCH

DB = "/home/makise-kurisu/.local/share/opencode/opencode.db"
LOG = "/home/makise-kurisu/projects/kurisu-todo/reconciler.log"
QUIET_RELEASE_MS = 30 * 60 * 1000
ACTIVE_WINDOW_MS = 10 * 60 * 1000
PROVIDER_NODE = {"glimmer-db01": "db01", "glimmer-db03": "db03", "glimmer-db04": "db04"}


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def session_facts():
    """child session id -> {node, finished, last_activity_ms}"""
    conn = sqlite3.connect(DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, model, idle_outcome, time_updated FROM session_v2 WHERE parent_id IS NOT NULL")
        facts = {}
        for sid, model_json, idle_outcome, t_upd in cur.fetchall():
            node = None
            if model_json:
                try:
                    node = PROVIDER_NODE.get(json.loads(model_json).get("providerID"))
                except Exception:
                    pass
            facts[sid] = {"node": node, "finished": idle_outcome is not None,
                          "last": t_upd or 0}
        try:
            cur.execute("SELECT session_id, MAX(time_updated) FROM session_message GROUP BY session_id")
            for sid, mx in cur.fetchall():
                if sid in facts and mx:
                    facts[sid]["last"] = max(facts[sid]["last"], mx)
        except Exception:
            pass
        return facts
    finally:
        conn.close()


def main():
    # serialize runs (timer + manual invocations must not double-release)
    import fcntl
    lock = open("/home/makise-kurisu/projects/kurisu-todo/.reconciler.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    orch = Client(ORCH)
    grants = orch.call("todo.list_grants", {})
    if isinstance(grants, str):
        log(f"ERROR list_grants: {grants[:160]}")
        return 1
    active = [g for g in grants if g["state"] in ("held", "bound")]
    if not active:
        return 0
    facts = session_facts()
    now_ms = int(time.time() * 1000)
    bound_sessions = {}
    for g in active:
        if g["state"] == "held":
            exp = g.get("expires_at")
            # expires_at is ISO; the server also lazily expires on
            # request/bind. Release here for a timely, recorded end.
            if exp:
                import datetime
                try:
                    exp_ms = int(datetime.datetime.fromisoformat(exp).timestamp() * 1000)
                except Exception:
                    exp_ms = None
                if exp_ms and now_ms > exp_ms:
                    r = orch.call("todo.release_slot", {"grant_id": g["id"], "reason": "held TTL expired unbound (reconciler)"})
                    log(f"released held grant {g['id'][:8]} node={g['node']} wo={g['work_order_id'][:8]}: TTL")
            continue
        sid = g.get("session_id")
        if sid:
            bound_sessions[sid] = g
        f = facts.get(sid) if sid else None
        if f is None:
            r = orch.call("todo.release_slot", {"grant_id": g["id"], "reason": "bound session not found in store (reconciler)"})
            log(f"released grant {g['id'][:8]} node={g['node']}: session {sid} absent from store -> {str(r)[:80]}")
        elif f["finished"]:
            r = orch.call("todo.release_slot", {"grant_id": g["id"], "reason": "session finished (reconciler)"})
            log(f"released grant {g['id'][:8]} node={g['node']}: session finished -> {str(r)[:80]}")
        elif now_ms - f["last"] > QUIET_RELEASE_MS:
            r = orch.call("todo.release_slot", {"grant_id": g["id"], "reason": "session quiet 30m - paused or stalled (reconciler)"})
            log(f"released grant {g['id'][:8]} node={g['node']}: quiet 30m -> {str(r)[:80]}")
    # anomaly scan: actively generating children with no bound grant
    for sid, f in facts.items():
        if not f["finished"] and now_ms - f["last"] < ACTIVE_WINDOW_MS and f["node"]:
            if sid not in bound_sessions:
                log(f"ANOMALY active session {sid} on {f['node']} has no bound grant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
