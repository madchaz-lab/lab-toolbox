#!/usr/bin/env python3
"""Chronoa weekly 6-hour watchdog (VM-local; madchaz's ruling, 2026-11-22).

The weekly paper run carries NO timeout: nothing kills it, however
long it takes on this hardware. If a chronoa-weekly job is still
running 6 hours after it started, this watchdog emails madchaz once
per job so a human hears about it. Read-only on the cluster; the only
side effect is that one email. State lives on this VM.

Usage: watchdog_6h.py [--check] [--test]
  --check  print every chronoa-weekly job's state and age; send nothing
  --test   send a test email to muse@ proving the mail path, then exit
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

NS = "brains-containment"
PREFIX = "chronoa-weekly-"
THRESHOLD = timedelta(hours=6)
STATE_DIR = os.path.expanduser("~/.local/share/chronoa-watchdog")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "watchdog.log")
JMAP = "http://192.168.27.210:8080/jmap/"
WELLKNOWN = "http://192.168.27.210:8080/.well-known/jmap"
TO_ADDR = "madchaz@local.madchaz.com"
TEST_ADDR = "muse@local.madchaz.com"
CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUB = "urn:ietf:params:jmap:submission"


def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    line = "%s %s" % (datetime.now(timezone.utc).isoformat(), msg)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def kubectl_json(args):
    r = subprocess.run(["kubectl", "-n", NS] + args,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("kubectl failed: " + r.stderr.strip()[:200])
    return json.loads(r.stdout)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"notified": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def mail_creds():
    r = subprocess.run(["kubectl", "-n", NS, "get", "secret",
                        "kurisu-mail-creds", "-o", "json"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("could not read kurisu-mail-creds")
    data = json.loads(r.stdout)["data"]
    return (base64.b64decode(data["username"]).decode(),
            base64.b64decode(data["password"]).decode())


def jmap_send(user, password, to, subject, body):
    """Mirror of the proven send flow in ~/projects/kurisu-mail/server.py."""
    auth = "Basic " + base64.b64encode(
        ("%s:%s" % (user, password)).encode()).decode()
    headers = {"Authorization": auth, "Content-Type": "application/json"}

    def get(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def call(method, args, using):
        payload = json.dumps(
            {"using": using, "methodCalls": [[method, args, "0"]]}).encode()
        req = urllib.request.Request(JMAP, data=payload, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        result = resp["methodResponses"][0][1]
        if isinstance(result, dict) and result.get("type") == "error":
            raise RuntimeError("JMAP error on %s: %s" % (method, result))
        return result

    sess = get(WELLKNOWN)
    account_id = sess["primaryAccounts"][MAIL]
    ids = call("Identity/get",
               {"accountId": account_id, "properties": ["id", "emailAddress"]},
               [CORE, SUB])["list"]
    identity_id = ids[0]["id"]
    from_email = ids[0].get("emailAddress") or user
    boxes = call("Mailbox/get",
                 {"accountId": account_id, "properties": ["id", "role"]},
                 [MAIL])["list"]
    drafts = next(m["id"] for m in boxes if m.get("role") == "drafts")
    sent = next(m["id"] for m in boxes if m.get("role") == "sent")
    created = call("Email/set", {"accountId": account_id, "create": {"w": {
        "mailboxIds": {drafts: True},
        "from": [{"email": from_email}],
        "to": [{"email": to}],
        "subject": subject,
        "textBody": [{"partId": "1", "type": "text/plain"}],
        "bodyValues": {"1": {"value": body}}}}}, [MAIL])
    if "w" in created.get("notCreated", {}):
        raise RuntimeError("Email/set refused: %s" % created["notCreated"]["w"])
    email_id = created["created"]["w"]["id"]
    submitted = call("EmailSubmission/set",
                     {"accountId": account_id, "create": {"s": {
                         "identityId": identity_id, "emailId": email_id}}},
                     [SUB])
    if "s" in submitted.get("notCreated", {}):
        raise RuntimeError("Submission refused: %s"
                           % submitted["notCreated"]["s"])
    call("Email/set", {"accountId": account_id,
                       "update": {email_id: {"mailboxIds": {sent: True}}}},
         [MAIL])
    return email_id


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def job_info(job):
    st = job.get("status", {})
    conds = {c["type"]: c["status"] for c in st.get("conditions", [])}
    finished = (conds.get("Complete") == "True"
                or conds.get("Failed") == "True"
                or bool(st.get("completionTime")))
    start_s = st.get("startTime") or job["metadata"]["creationTimestamp"]
    return finished, parse_ts(start_s)


def pod_summary(job_name):
    try:
        pods = kubectl_json(["get", "pods", "-l", "job-name=" + job_name,
                             "-o", "json"])["items"]
    except Exception:
        return "pod lookup failed"
    if not pods:
        return "no pod found"
    p = pods[0]
    return "pod %s on %s, phase %s" % (
        p["metadata"]["name"], p["spec"].get("nodeName", "?"),
        p["status"].get("phase", "?"))


def main():
    if "--test" in sys.argv:
        user, password = mail_creds()
        eid = jmap_send(user, password, TEST_ADDR,
                        "[TEST] Chronoa 6h watchdog - email path check",
                        "This is a test of the Chronoa weekly watchdog's "
                        "email path, sent at setup time (2026-11-22).\n\n"
                        "The real alert goes to madchaz@ if a "
                        "chronoa-weekly job is still running 6 hours "
                        "after it starts. The run itself has no timeout; "
                        "nothing kills it.\n")
        log("test email sent to %s (id %s)" % (TEST_ADDR, eid))
        return 0

    jobs = kubectl_json(["get", "jobs", "-o", "json"])["items"]
    now = datetime.now(timezone.utc)
    state = load_state()
    checked = 0
    for job in jobs:
        name = job["metadata"]["name"]
        if not name.startswith(PREFIX):
            continue
        checked += 1
        finished, start = job_info(job)
        age = now - start
        if "--check" in sys.argv:
            print("%s state=%s age=%.2fh uid=%s" % (
                name, "finished" if finished else "running",
                age.total_seconds() / 3600.0, job["metadata"]["uid"]))
            continue
        if finished or age < THRESHOLD:
            continue
        uid = job["metadata"]["uid"]
        if uid in state["notified"]:
            continue
        subject = "Chronoa weekly still running after 6 hours - %s" % name
        body = (
            "The Chronoa weekly paper run is still going after 6 hours.\n"
            "\n"
            "Job: %s\n"
            "Started: %s UTC\n"
            "Elapsed: %.1f hours\n"
            "%s\n"
            "\n"
            "Nothing will kill it - the run carries no timeout, by your "
            "ruling; it runs until the Newsroom finishes or something "
            "actually breaks. While it runs, the CronJob's Forbid policy "
            "holds any later weekly fire, and the issue state advances "
            "only on success.\n"
            "\n"
            "- chronoa watchdog (Kurisu's VM)\n"
            % (name, start.isoformat(), age.total_seconds() / 3600.0,
               pod_summary(name)))
        user, password = mail_creds()
        eid = jmap_send(user, password, TO_ADDR, subject, body)
        state["notified"][uid] = now.isoformat()
        save_state(state)
        log("alert sent for %s (email id %s)" % (name, eid))
    if "--check" not in sys.argv:
        log("check complete: %d weekly job(s) seen" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
