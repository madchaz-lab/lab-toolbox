#!/usr/bin/env bash
set -euo pipefail

LOGFILE="/var/log/kurisu-heartbeat.log"
LOCKFILE="/var/lock/kurisu-heartbeat.lock"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "$(date -Iseconds) lock held" >> "$LOGFILE"; exit 0; }

TIMESTAMP=$(date -Iseconds)

# Busy check: the opencode store is the only real signal, and session_v2's
# own bookkeeping is not it - twice on 2026-11-19 the gate read idle over live
# work (a suspended row's fresh time_updated faked activity in the morning;
# in the evening a running child's row and the orchestrator's row both sat
# still for over a minute mid-turn while her messages moved continuously).
# The durable signal is message activity: while any session is mid-turn, its
# messages keep landing. Busy = a running child (no outcome, not suspended),
# OR any session_message updated in the last 240s, OR the orchestrator's last
# turn ended 'interrupted' in the last 1800s (madchaz is in the session
# driving her himself, at human pace - waking then barges into his
# conversation; corrected by madchaz 2026-11-19 after exactly that happened),
# OR any non-suspended session row updated in the last 900s (loose fallback).
# A suspended worker produces no messages, so a stall still surfaces ~4 min
# after the work stops moving.
BUSY=$(python3 - << 'PYEOF'
import sqlite3, time
try:
    db = sqlite3.connect("/home/makise-kurisu/.local/share/opencode/opencode.db")
    now_ms = int(time.time() * 1000)
    running = db.execute("select count(*) from session_v2 where parent_id is not null and idle_outcome is null and time_suspended is null").fetchone()[0]
    msg_latest = db.execute("select max(time_updated) from session_message").fetchone()[0] or 0
    orch = db.execute("select idle_outcome, time_updated from session_v2 where parent_id is null order by time_updated desc limit 1").fetchone()
    latest = db.execute("select max(time_updated) from session_v2 where time_suspended is null").fetchone()[0] or 0
    if running > 0:
        print("child_running")
    elif now_ms - msg_latest < 240000:
        print("message_active")
    elif orch and orch[0] == "interrupted" and now_ms - orch[1] < 1800000:
        print("user_driving")
    elif now_ms - latest < 900000:
        print("session_active")
    else:
        print("idle")
except Exception:
    print("unknown")
PYEOF
)
case "$BUSY" in
  child_running|session_active|user_driving|message_active)
    echo "$TIMESTAMP busy $BUSY" >> "$LOGFILE"
    exit 0
    ;;
  unknown)
    echo "$TIMESTAMP busy_check_failed" >> "$LOGFILE"
    exit 0
    ;;
esac

# Work check: open or in-progress WORK ORDERS are the actionable unit.
# (v2, 2026-11-18: v1 counted open tasks only and could sit quiet while an
# in-progress task still held open orders.)
# (v3, 2026-11-22, madchaz's direction: done_proposed orders count too -
# a proposal awaiting acceptance/validation is orchestrator work - and a
# READY STEP with no live work order counts as well: its dependencies are
# done, its task/project are live, and creating its work order is the
# orchestrator's next act. v2's orders-only test sat silent for hours on
# 2026-11-22 while the researcher probe step stood ready with no order
# under it and the orchestrator waited on a question.)
DB_USER="kurisu"
DB_NAME="kurisu"
POD=$(kubectl get pods -n brains-containment -l app=kurisu-postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -z "$POD" ]; then
  echo "$TIMESTAMP board_check_failed" >> "$LOGFILE"
  exit 0
fi
ORDER_COUNT=$(kubectl exec -n brains-containment "$POD" -- psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM todo_work_orders WHERE status IN ('open','in_progress','done_proposed');" 2>/dev/null | tr -d " " || true)
if [ -z "$ORDER_COUNT" ]; then
  echo "$TIMESTAMP board_check_failed" >> "$LOGFILE"
  exit 0
fi

# Wake-reason detail (2026-11-19, madchaz's direction): name the actual work,
# not just the count - in-progress orders first, then most recently updated,
# so a wake reads as a resume brief instead of sending her to check the board.
ORDER_DETAIL=$(kubectl exec -n brains-containment "$POD" -- psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -c "SELECT string_agg(x, '; ') FROM (SELECT regexp_replace(w.name, '\s+', ' ', 'g') || ' (' || p.name || ')' AS x FROM todo_work_orders w JOIN todo_steps s ON s.id = w.step_id JOIN todo_tasks t ON t.id = s.task_id JOIN todo_projects p ON p.id = t.project_id WHERE w.status IN ('open','in_progress','done_proposed') ORDER BY CASE w.status WHEN 'in_progress' THEN 0 ELSE 1 END, w.updated_at DESC LIMIT 3) q;" 2>/dev/null || true)

# Ready-step check (v3): steps mirroring the todo tool's own readiness -
# every depends_on step 'done' (dependencies_done), parents live, and no
# work order in a live state under the step. The tool's dispatch gate
# still governs the actual dispatch; this only decides the wake.
STEP_PRED="s.status IN ('open','in_progress') AND t.status NOT IN ('done','cancelled') AND p.status NOT IN ('done','cancelled') AND NOT EXISTS (SELECT 1 FROM unnest(s.depends_on) d JOIN todo_steps ds ON ds.id = d WHERE ds.status <> 'done') AND NOT EXISTS (SELECT 1 FROM todo_work_orders w WHERE w.step_id = s.id AND w.status IN ('open','in_progress','done_proposed'))"
STEP_COUNT=$(kubectl exec -n brains-containment "$POD" -- psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM todo_steps s JOIN todo_tasks t ON t.id = s.task_id JOIN todo_projects p ON p.id = t.project_id WHERE $STEP_PRED;" 2>/dev/null | tr -d " " || true)
if [ -z "$STEP_COUNT" ]; then
  echo "$TIMESTAMP board_check_failed" >> "$LOGFILE"
  exit 0
fi
STEP_DETAIL=$(kubectl exec -n brains-containment "$POD" -- psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -c "SELECT string_agg(x, '; ') FROM (SELECT regexp_replace(s.name, '\s+', ' ', 'g') || ' (' || t.name || ' / ' || p.name || ')' AS x FROM todo_steps s JOIN todo_tasks t ON t.id = s.task_id JOIN todo_projects p ON p.id = t.project_id WHERE $STEP_PRED ORDER BY s.updated_at DESC LIMIT 3) q;" 2>/dev/null || true)

# Mail check (v2, 2026-11-18, madchaz's direction): unread mail in her inbox is
# also a wake reason - it is how review replies and breakpoint traffic reach her
# between sessions. Read-only IMAP probe; marks nothing seen. On any failure the
# count is "unknown" and mail simply does not contribute to this wake.
MAIL=$(python3 - << 'PYEOF'
import imaplib, subprocess, json, base64
try:
    sec = json.loads(subprocess.run(["kubectl", "-n", "brains-containment", "get", "secret", "kurisu-mail-creds", "-o", "json"], capture_output=True, text=True, timeout=30).stdout)
    user = base64.b64decode(sec["data"]["username"]).decode()
    pw = base64.b64decode(sec["data"]["password"]).decode()
    m = imaplib.IMAP4_SSL("192.168.27.210", 993, timeout=20)
    m.login(user, pw)
    m.select("INBOX", readonly=True)
    typ, data = m.search(None, "UNSEEN")
    ids = data[0].split() if data and data[0] else []
    print(len(ids))
    if ids:
        import email
        from email.header import decode_header
        def clean(v):
            parts = []
            for b, enc in decode_header(v or ""):
                parts.append(b.decode(enc or "utf-8", "replace") if isinstance(b, bytes) else b)
            s = " ".join("".join(parts).split())
            return s.replace(chr(34), "'").replace("\\", "/")[:120]
        typ, hdr = m.fetch(ids[-1], "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        msg = email.message_from_bytes(hdr[0][1])
        print("newest: " + clean(msg.get("From")) + " - " + clean(msg.get("Subject")))
    m.logout()
except Exception:
    print("unknown")
PYEOF
)

UNREAD=$(printf '%s' "$MAIL" | head -n 1)
MAIL_DETAIL=$(printf '%s' "$MAIL" | tail -n +2 | head -n 1)

if [ "$ORDER_COUNT" -eq 0 ] && [ "$STEP_COUNT" -eq 0 ] && [ "$UNREAD" = "0" ]; then
  echo "$TIMESTAMP idle no_work no_mail" >> "$LOGFILE"
  exit 0
fi
if [ "$ORDER_COUNT" -eq 0 ] && [ "$STEP_COUNT" -eq 0 ] && [ "$UNREAD" = "unknown" ]; then
  echo "$TIMESTAMP idle no_work mail_unknown" >> "$LOGFILE"
  exit 0
fi

TMUX_SESSION="kurisu"
WAKE_TEXT="[HEARTBEAT - BACKSTOP] Wake reason:"
if [ "$ORDER_COUNT" -gt 0 ]; then
  if [ -n "$ORDER_DETAIL" ]; then
    WAKE_TEXT="$WAKE_TEXT $ORDER_COUNT work order(s) open or in progress - $ORDER_DETAIL."
  else
    WAKE_TEXT="$WAKE_TEXT $ORDER_COUNT work order(s) open or in progress."
  fi
fi
if [ "$STEP_COUNT" -gt 0 ]; then
  if [ -n "$STEP_DETAIL" ]; then
    WAKE_TEXT="$WAKE_TEXT $STEP_COUNT ready step(s) awaiting work orders - $STEP_DETAIL."
  else
    WAKE_TEXT="$WAKE_TEXT $STEP_COUNT ready step(s) awaiting work orders."
  fi
fi
if [ "$UNREAD" != "unknown" ] && [ "$UNREAD" -gt 0 ]; then
  if [ -n "$MAIL_DETAIL" ]; then
    WAKE_TEXT="$WAKE_TEXT $UNREAD unread email(s) in your inbox - $MAIL_DETAIL - read mail early this wake."
  else
    WAKE_TEXT="$WAKE_TEXT $UNREAD unread email(s) in your inbox - read mail early this wake."
  fi
fi
WAKE_TEXT="$WAKE_TEXT The timer woke you; no worker return did - the loop stalled, or you ended a turn into a wait. If an unresolved breakpoint stands, remain halted and answer with the breakpoint and its fresh citation. Otherwise resume the loop from the board."

if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux send-keys -t "${TMUX_SESSION}:0" "$WAKE_TEXT" Enter
  echo "$TIMESTAMP wake_sent orders=$ORDER_COUNT steps=$STEP_COUNT unread=$UNREAD" >> "$LOGFILE"
else
  echo "$TIMESTAMP error tmux_missing" >> "$LOGFILE"
fi

exit 0
