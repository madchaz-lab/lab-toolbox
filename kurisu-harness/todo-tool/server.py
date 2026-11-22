import os
import json
import hmac
import hashlib
import base64
import time
import functools
import contextvars
from typing import Optional, Dict, Any, List
import psycopg
from psycopg.rows import dict_row
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

mcp = FastMCP("kurisu-todo")

TODO_TOKEN_SECRET = os.getenv("TODO_TOKEN_SECRET")
TODO_PORT = int(os.getenv("TODO_PORT", "8000"))

if not TODO_TOKEN_SECRET:
    raise SystemExit("TODO_TOKEN_SECRET environment variable is required")

# DSN construction with percent-encoding for password safety
TODO_DSN = os.getenv("TODO_DSN")
if TODO_DSN:
    DSN = TODO_DSN
else:
    # discrete parts from Secret kurisu-postgres-creds
    PG_HOST = os.getenv("TODO_PGHOST")
    PG_PORT = os.getenv("TODO_PGPORT", "5432")
    PG_USER = os.getenv("TODO_PGUSER")
    PG_PASSWORD = os.getenv("TODO_PGPASSWORD")
    PG_DATABASE = os.getenv("TODO_PGDATABASE")
    if not all([PG_HOST, PG_USER, PG_PASSWORD, PG_DATABASE]):
        raise SystemExit("TODO_DSN or TODO_PGHOST/TODO_PGUSER/TODO_PGPASSWORD/TODO_PGDATABASE must be set")
    from urllib.parse import quote_plus
    user_enc = quote_plus(PG_USER)
    pass_enc = quote_plus(PG_PASSWORD)
    DSN = f"postgresql://{user_enc}:{pass_enc}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"

from contextlib import contextmanager
from psycopg_pool import ConnectionPool

# 2026-11-19 (Muse, review fix at madchaz's direction): pooled connections.
# The original get_conn opened a brand-new connection for every helper call;
# multiplied by a recursive caller that wedged this server once already.
# The double context preserves the old semantics exactly: call sites use
# `with get_conn() as conn`, and the inner `with conn` keeps
# commit-on-success / rollback-on-error per block.
_pool = ConnectionPool(DSN, min_size=1, max_size=8,
                        kwargs={"row_factory": dict_row}, open=False)
_pool.open(wait=True)


@contextmanager
def get_conn():
    with _pool.connection() as conn:
        with conn:
            yield conn

# --- WS1 foundation (2026-11-21, Muse, pause window, madchaz direction) ---
# Versioned, idempotent migrations applied at startup and recorded as prose
# rows in todo_migrations (key-prefixed for reliable idempotency checks).
# A full pg_dump precedes this deploy: ~/backups/todo-pre-ws1-2026-11-21/.
MIGRATIONS = [
    ("ws1-foundation-2026-11-21",
     "WS1 foundation: revision counters on todo_projects/todo_tasks/todo_steps/todo_work_orders; metadata jsonb on todo_workflow_template_versions; todo_guard_denials table (monotonic guard record: denials are append-only rows, cleared only by an explicit recorded action); programming-build v1 metadata backfilled audit_required=true.",
     [
        "ALTER TABLE todo_projects ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1",
        "ALTER TABLE todo_tasks ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1",
        "ALTER TABLE todo_steps ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1",
        "ALTER TABLE todo_work_orders ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1",
        "ALTER TABLE todo_workflow_template_versions ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
        "CREATE TABLE IF NOT EXISTS todo_guard_denials (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), entity_type text NOT NULL, entity_id uuid NOT NULL, guard text NOT NULL, verb text NOT NULL, reason text NOT NULL, evidence jsonb, status text NOT NULL DEFAULT 'active', created_at timestamptz NOT NULL DEFAULT now(), cleared_at timestamptz, cleared_by text)",
        "UPDATE todo_workflow_template_versions v SET metadata = '{\"audit_required\": true}'::jsonb FROM todo_workflow_templates t WHERE v.template_id = t.id AND t.name = 'programming-build' AND v.metadata = '{}'::jsonb",
     ]),
    ("ws2-slot-grants-2026-11-22",
     "WS2 slot grants: todo_slot_grants ledger (the dispatch cap made mechanical). One active grant per node and per work order via partial unique indexes. The six standalone slot modules in ~/reports were audited and replaced (in-memory ledger, placeholder validation, wrong running-definition); design in ~/pause-window/ws2-spec.md.",
     [
        "CREATE TABLE IF NOT EXISTS todo_slot_grants (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), work_order_id uuid NOT NULL, node text NOT NULL, session_id text, mode text NOT NULL DEFAULT 'dispatch', state text NOT NULL DEFAULT 'held', created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, bound_at timestamptz, released_at timestamptz, release_reason text)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_grants_active_node ON todo_slot_grants (node) WHERE state IN ('held','bound')",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_grants_active_wo ON todo_slot_grants (work_order_id) WHERE state IN ('held','bound')",
     ]),
    ("fix-agent-load-view-2026-11-22",
     "agent_load view fix (third review, Muse at madchaz's direction): the view counted only open/in_progress/done and filtered to assigned orders - done_proposed orders (awaiting acceptance/validation) and unassigned orders were invisible in the orchestrator's one load view. New column done_proposed_tasks appended (CREATE OR REPLACE VIEW compatibility); the assigned_worker filter is dropped so unassigned orders appear as a null-worker row.",
     [
        "CREATE OR REPLACE VIEW todo_agent_load AS SELECT assigned_worker, count(*) FILTER (WHERE status = 'open') AS open_tasks, count(*) FILTER (WHERE status = 'in_progress') AS in_progress_tasks, count(*) FILTER (WHERE status = 'done') AS done_tasks, now() AS updated_at, count(*) FILTER (WHERE status = 'done_proposed') AS done_proposed_tasks FROM todo_work_orders GROUP BY assigned_worker",
     ]),
]

def run_migrations():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for key, desc, stmts in MIGRATIONS:
                cur.execute("SELECT 1 FROM todo_migrations WHERE description LIKE %s", (key + ":%",))
                if cur.fetchone():
                    continue
                for s in stmts:
                    cur.execute(s)
                cur.execute("INSERT INTO todo_migrations (description) VALUES (%s)",
                            (f"{key}: {desc}",))

run_migrations()

# --- Guard framework -----------------------------------------------------
# Deny-only, monotonic: a guard can refuse a transition, never permit one
# that the verb itself would refuse. A denial is recorded in
# todo_guard_denials (append-only) and as a comment on the entity; it is
# never auto-cleared. Guards register per verb in GUARDS; WS4 activates
# the concrete guards. A guard receives (entity_type, entity_id, ctx) and
# raises GuardDenied to refuse.
class GuardDenied(Exception):
    def __init__(self, guard, reason, evidence=None):
        super().__init__(f"guard denied [{guard}]: {reason}")
        self.guard = guard
        self.reason = reason
        self.evidence = evidence or {}

GUARDS = {
    "validate_done": [],
    "propose_done": [],
    "propose_step_done": [],
    "cancel_element": [],
    "add_template_version": [],
    "create_work_order": [],
}

def run_guards(verb, entity_type, entity_id, ctx=None):
    for g in GUARDS.get(verb, []):
        g(entity_type, entity_id, ctx or {})

def guarded(verb, entity_type, entity_id, ctx=None):
    try:
        run_guards(verb, entity_type, entity_id, ctx)
    except GuardDenied as d:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO todo_guard_denials (entity_type, entity_id, guard, verb, reason, evidence) VALUES (%s,%s,%s,%s,%s,%s)",
                    (entity_type, entity_id, d.guard, verb, d.reason, json.dumps(d.evidence)))
        insert_comment(entity_type, entity_id, "guard",
                       f"GUARD DENIED [{d.guard}] on {verb}: {d.reason}")
        raise ValueError(str(d))

# --- Mutation revisions (compare-and-swap) --------------------------------
# Every status-changing write bumps the entity's revision. A caller that
# passes expected_revision gets a refusal when its view is stale: the
# revision is checked fresh at verb entry and bumped by the write itself.
def check_revision(entity_type, entity_id, expected_revision):
    if expected_revision is None:
        return
    e = get_entity(entity_type, entity_id)
    if not e:
        raise ValueError("not found")
    if e.get("revision") != expected_revision:
        raise ValueError(
            f"revision mismatch on {entity_type} {entity_id}: caller expected "
            f"{expected_revision}, current is {e.get('revision')} - re-read and retry")

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)

def mint_token(cls: str, binding: Optional[Dict[str, Any]] = None, ttl: int = 3600) -> str:
    payload = {
        "cls": cls,
        "binding": binding or {},
        "exp": int(time.time()) + ttl,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(TODO_TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"

def verify_token(token: str) -> Dict[str, Any]:
    try:
        payload_b64, sig_b64 = token.split(".")
        sig = _b64url_decode(sig_b64)
        expected = hmac.new(TODO_TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("invalid signature")
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("token expired")
        return payload
    except Exception as e:
        raise ValueError(f"invalid token: {e}")

_ARG_AUTH = contextvars.ContextVar("todo_arg_auth", default=None)

def call_token(fn):
    """Batch 7 (2026-11-22, madchaz's direction): the class-gated verbs
    accept the acting token as a call argument, so a session whose
    connection carries one fixed credential (the orchestrator's) can
    perform worker / step_owner / validator acts on the same surface -
    no second connection, no hand-rolled client. The token is verified
    BEFORE the verb runs and, for the duration of the call, get_auth()
    resolves to it: every class check, binding check and actor
    attribution inside the verb sees the token's class, exactly as if
    it had arrived on its own connection. Fail-closed: an invalid or
    expired token aborts the call with the verification error; there
    is no fallback to the connection's header identity. With no token
    argument, behavior is unchanged (headers decide)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = kwargs.pop("token", None)
        if token is None:
            return fn(*args, **kwargs)
        auth = verify_token(token)
        reset = _ARG_AUTH.set(auth)
        try:
            return fn(*args, **kwargs)
        finally:
            _ARG_AUTH.reset(reset)
    return wrapper

def get_auth():
    override = _ARG_AUTH.get()
    if override is not None:
        return override
    try:
        req = get_http_request()
        headers = {k.lower(): v for k, v in req.headers.items()}
        token = headers.get("x-worker-token") or headers.get("x-orchestrator-token") or headers.get("x-validator-token")
        if not token:
            return None
        return verify_token(token)
    except Exception:
        return None

# Ontology (madchaz, 2026-11-18): project = goal; workflow = template (versioned);
# task = one instance of a workflow; step = gate inside a task; work order = one
# delegation inside a step (one worker, one session, one verifiable artifact).
ALLOWED_ENTITY_TYPES = {"project", "task", "step", "work_order"}
COMMENTABLE_ENTITY_TYPES = ALLOWED_ENTITY_TYPES | {"template"}
ALLOWED_AGENTS = {
    "orchestrator",
    "kurisu-researcher",
    "kurisu-programming",
    "kurisu-labadmin",
    "kurisu-conversation",
    "kurisu-quality",
    "kurisu-documentation",
    "kurisu-security",
}

def entity_table(entity_type: str):
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise ValueError("invalid entity type")
    return {
        "project": "todo_projects",
        "task": "todo_tasks",
        "step": "todo_steps",
        "work_order": "todo_work_orders",
    }[entity_type]

def insert_comment(entity_type: str, entity_id: str, author: str, body: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_comments (entity_type, entity_id, author, body) VALUES (%s,%s,%s,%s)",
                (entity_type, entity_id, author, body)
            )

def write_transition(conn, entity_type: str, entity_id: str, from_status: Optional[str], to_status: str, actor_class: str, actor_id: Optional[str] = None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO todo_transition_events (entity_type, entity_id, from_status, to_status, actor_class, actor_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (entity_type, entity_id, from_status, to_status, actor_class, actor_id)
        )

def get_entity(entity_type: str, entity_id: str):
    table = entity_table(entity_type)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id=%s", (entity_id,))
            return cur.fetchone()

def update_metadata(entity_type: str, entity_id: str, patch: Dict[str, Any]):
    """Shallow-merge patch into the entity's metadata, atomically at the
    row level (pass-4 fix, 2026-11-22): the previous read-modify-write
    raced across actors - the executor's probe records and the
    orchestrator's acceptance write to the same work order's metadata
    from different processes, and a lost update could silently drop
    accepted_by or a probe record. jsonb || is the same shallow merge,
    performed by the database in one statement."""
    table = entity_table(entity_type)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET metadata = COALESCE(metadata, '{{}}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s",
                        (json.dumps(patch), entity_id))

def validate_depends_on(entity_type: str, depends_on: List[str]):
    if not depends_on:
        return True
    table = entity_table(entity_type)
    with get_conn() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"]*len(depends_on))
            cur.execute(f"SELECT id FROM {table} WHERE id IN ({placeholders})", depends_on)
            found = {str(r["id"]) for r in cur.fetchall()}
            missing = set(str(d) for d in depends_on) - found
            if missing:
                raise ValueError(f"depends_on references missing ids: {missing}")
    return True

def dependencies_done(entity_type: str, entity_id: str) -> bool:
    entity = get_entity(entity_type, entity_id)
    if not entity:
        return False
    depends_on = entity.get("depends_on") or []
    if not depends_on:
        return True
    table = entity_table(entity_type)
    with get_conn() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"]*len(depends_on))
            cur.execute(f"SELECT id, status FROM {table} WHERE id IN ({placeholders})", depends_on)
            rows = cur.fetchall()
            # distinct-count: a duplicated id in depends_on must not make
            # this permanently False (the IN query returns distinct rows)
            if len(rows) != len({str(d) for d in depends_on}):
                return False
            for r in rows:
                if r["status"] != "done":
                    return False
    return True

def check_status(entity_type: str, entity_id: str, expected: str):
    e = get_entity(entity_type, entity_id)
    if not e:
        raise ValueError("not found")
    if e["status"] != expected:
        raise ValueError(f"status precondition failed: expected {expected}, got {e['status']}")

def get_children(entity_type: str, entity_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if entity_type == "project":
                cur.execute("SELECT id, status FROM todo_tasks WHERE project_id=%s", (entity_id,))
            elif entity_type == "task":
                cur.execute("SELECT id, status FROM todo_steps WHERE task_id=%s", (entity_id,))
            elif entity_type == "step":
                cur.execute("SELECT id, status FROM todo_work_orders WHERE step_id=%s", (entity_id,))
            else:
                return []
            return cur.fetchall()

def child_type_for(parent_type: str) -> str:
    mapping = {
        "project": "task",
        "task": "step",
        "step": "work_order",
    }
    return mapping.get(parent_type)

# --- Workflow templates -------------------------------------------------

def validate_dag(step_dag: Any):
    """A template step DAG: [{key, name, owner, depends_on:[keys], acceptance}].
    Every step: unique key, one allowlisted owner, one non-empty acceptance,
    dependencies that resolve inside the DAG, and no cycles."""
    if not isinstance(step_dag, list) or not step_dag:
        raise ValueError("step_dag must be a non-empty list of steps")
    keys = []
    for s in step_dag:
        if not isinstance(s, dict):
            raise ValueError("each step must be an object")
        key = s.get("key")
        if not key or not isinstance(key, str):
            raise ValueError("each step needs a string key")
        if key in keys:
            raise ValueError(f"duplicate step key: {key}")
        keys.append(key)
        if not s.get("name"):
            raise ValueError(f"step {key}: name required")
        if s.get("owner") not in ALLOWED_AGENTS:
            raise ValueError(f"step {key}: owner must be one of {sorted(ALLOWED_AGENTS)}")
        if not s.get("acceptance"):
            raise ValueError(f"step {key}: acceptance text required (a step is a gate with one acceptance check)")
        for dep in s.get("depends_on") or []:
            if dep not in keys and dep not in [x.get("key") for x in step_dag]:
                raise ValueError(f"step {key}: depends_on references unknown key {dep}")
    # acyclicity (Kahn)
    deps = {s["key"]: list(s.get("depends_on") or []) for s in step_dag}
    resolved = set()
    progress = True
    while progress:
        progress = False
        for k, ds in deps.items():
            if k not in resolved and all(d in resolved for d in ds):
                resolved.add(k)
                progress = True
    if len(resolved) != len(deps):
        raise ValueError("step_dag has a dependency cycle")
    return True

def get_template_row(name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_workflow_templates WHERE name=%s", (name,))
            return cur.fetchone()

def get_template_version(template_id: str, version: Optional[int] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if version is None:
                cur.execute(
                    "SELECT * FROM todo_workflow_template_versions WHERE template_id=%s ORDER BY version DESC LIMIT 1",
                    (template_id,))
            else:
                cur.execute(
                    "SELECT * FROM todo_workflow_template_versions WHERE template_id=%s AND version=%s",
                    (template_id, version))
            return cur.fetchone()

# --- Tools: templates ---------------------------------------------------

@mcp.tool(name="todo.create_template")
def create_template(name: str, step_dag: List[Dict[str, Any]], description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    validate_dag(step_dag)
    if get_template_row(name):
        raise ValueError(f"template already exists: {name} (add a new version instead)")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_workflow_templates (name, description) VALUES (%s,%s) RETURNING id",
                (name, description))
            tid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO todo_workflow_template_versions (template_id, version, step_dag, metadata) VALUES (%s,1,%s,%s)",
                (tid, json.dumps(step_dag), json.dumps(metadata or {})))
    return {"id": str(tid), "name": name, "version": 1}

@mcp.tool(name="todo.add_template_version")
def add_template_version(name: str, step_dag: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    validate_dag(step_dag)
    tpl = get_template_row(name)
    if not tpl:
        raise ValueError(f"template not found: {name}")
    latest = get_template_version(str(tpl["id"]))
    new_version = (latest["version"] + 1) if latest else 1
    guarded("add_template_version", "template", str(tpl["id"]),
            {"name": name, "new_version": new_version,
             "new_keys": [s.get("key") for s in step_dag]})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_workflow_template_versions (template_id, version, step_dag, metadata) VALUES (%s,%s,%s,%s)",
                (tpl["id"], new_version, json.dumps(step_dag), json.dumps(metadata or {})))
    return {"id": str(tpl["id"]), "name": name, "version": new_version}

@mcp.tool(name="todo.list_templates")
def list_templates():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.id, t.name, t.description,
                          (SELECT MAX(v.version) FROM todo_workflow_template_versions v WHERE v.template_id = t.id) AS latest_version,
                          (SELECT COUNT(*) FROM todo_workflow_template_versions v WHERE v.template_id = t.id) AS versions
                   FROM todo_workflow_templates t ORDER BY t.name""")
            return cur.fetchall()

@mcp.tool(name="todo.get_template")
def get_template(name: str, version: Optional[int] = None):
    tpl = get_template_row(name)
    if not tpl:
        raise ValueError(f"template not found: {name}")
    ver = get_template_version(str(tpl["id"]), version)
    if not ver:
        raise ValueError(f"template version not found: {name} v{version}")
    dag = ver["step_dag"]
    if isinstance(dag, str):
        dag = json.loads(dag)
    vmeta = ver.get("metadata") or {}
    if isinstance(vmeta, str):
        vmeta = json.loads(vmeta)
    return {"name": name, "version": ver["version"], "step_dag": dag, "metadata": vmeta}

@mcp.tool(name="todo.instantiate_task")
def instantiate_task(project_id: str, template_name: str, name: str, version: Optional[int] = None, bank_id: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    tpl = get_template_row(template_name)
    if not tpl:
        raise ValueError(f"template not found: {template_name}")
    ver = get_template_version(str(tpl["id"]), version)
    if not ver:
        raise ValueError(f"template version not found: {template_name} v{version}")
    dag = ver["step_dag"]
    if isinstance(dag, str):
        dag = json.loads(dag)
    validate_dag(dag)
    vmeta = ver.get("metadata") or {}
    if isinstance(vmeta, str):
        vmeta = json.loads(vmeta)
    audit_required = vmeta.get("audit_required")
    if audit_required is None:
        audit_required = (template_name == "programming-build")
    meta = {"template": template_name, "template_version": ver["version"], "deviations": [],
            "template_metadata": vmeta, "audit_required": bool(audit_required)}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_tasks (project_id, name, bank_id, status, template_id, template_version, metadata) VALUES (%s,%s,%s,'open',%s,%s,%s) RETURNING id",
                (project_id, name, bank_id, tpl["id"], ver["version"], json.dumps(meta)))
            task_id = cur.fetchone()["id"]
            key_to_id = {}
            for idx, s in enumerate(dag):
                smeta = {"agent": s["owner"], "key": s["key"], "acceptance": s["acceptance"], "template_step": True}
                cur.execute(
                    "INSERT INTO todo_steps (task_id, name, order_idx, status, depends_on, metadata) VALUES (%s,%s,%s,'open','{}',%s) RETURNING id",
                    (task_id, s["name"], idx, json.dumps(smeta)))
                key_to_id[s["key"]] = cur.fetchone()["id"]
            for s in dag:
                deps = [str(key_to_id[d]) for d in (s.get("depends_on") or [])]
                if deps:
                    cur.execute(
                        "UPDATE todo_steps SET depends_on=%s WHERE id=%s",
                        (deps, key_to_id[s["key"]]))
    return {"id": str(task_id), "template": template_name, "template_version": ver["version"],
            "steps": {k: str(v) for k, v in key_to_id.items()}}

# --- Tools: structure ---------------------------------------------------

@mcp.tool(name="todo.create_project")
def create_project(name: str, description: Optional[str] = None, bank_id: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_projects (name, description, bank_id, status) VALUES (%s,%s,%s,'open') RETURNING id",
                (name, description, bank_id))
            pid = cur.fetchone()["id"]
    return {"id": str(pid)}

@mcp.tool(name="todo.create_task_instance")
def create_task_instance(project_id: str, name: str, bank_id: Optional[str] = None, depends_on: Optional[List[str]] = None):
    """An ad-hoc task (instance with no template). For templated work, use
    todo.instantiate_task so the instance carries its template stamp."""
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    depends_on = depends_on or []
    validate_depends_on("task", depends_on)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_tasks (project_id, name, bank_id, status, depends_on) VALUES (%s,%s,%s,'open',%s) RETURNING id",
                (project_id, name, bank_id, depends_on))
            tid = cur.fetchone()["id"]
    return {"id": str(tid)}

@mcp.tool(name="todo.create_step")
def create_step(task_id: str, name: str, order_idx: int = 0, depends_on: Optional[List[str]] = None, agent: Optional[str] = None, key: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    depends_on = depends_on or []
    validate_depends_on("step", depends_on)
    if key:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata FROM todo_steps WHERE task_id=%s AND status<>'cancelled'", (task_id,))
                for r in cur.fetchall():
                    if _meta_of(r).get("key") == key:
                        raise ValueError(f"step key '{key}' already exists on a live step of this task")
    meta = {}
    if agent:
        meta["agent"] = agent
    if key:
        meta["key"] = key
    metadata = json.dumps(meta) if meta else "{}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_steps (task_id, name, order_idx, status, depends_on, metadata) VALUES (%s,%s,%s,'open',%s,%s) RETURNING id",
                (task_id, name, order_idx, depends_on, metadata))
            sid = cur.fetchone()["id"]
    # a step added to a templated task after instantiation is a deviation: record it
    task = get_entity("task", task_id)
    if task and task.get("template_id"):
        meta = task.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        deviations = meta.get("deviations") or []
        deviations.append({"action": "step_added", "step_id": str(sid), "name": name,
                           "at": int(time.time())})
        update_metadata("task", task_id, {"deviations": deviations})
    return {"id": str(sid)}

@mcp.tool(name="todo.cancel_element")
def cancel_element(entity_type: str, entity_id: str, reason: str, expected_revision: Optional[int] = None, force: bool = False):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise ValueError("invalid entity type")
    entity = get_entity(entity_type, entity_id)
    if not entity:
        raise ValueError("entity not found")
    if entity["status"] in ("done", "cancelled"):
        raise ValueError(f"cannot cancel element in status {entity['status']}")
    check_revision(entity_type, entity_id, expected_revision)
    guarded("cancel_element", entity_type, entity_id, {"reason": reason, "force": force})
    from_status = entity["status"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {entity_table(entity_type)} SET status='cancelled', revision=revision+1, updated_at=now() WHERE id=%s", (entity_id,))
            write_transition(conn, entity_type, entity_id, from_status, "cancelled", "orchestrator")
    insert_comment(entity_type, entity_id, "orchestrator", f"cancelled: {reason}")
    if entity_type == "step":
        task = get_entity("task", str(entity["task_id"]))
        if task and task.get("template_id"):
            meta = task.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            deviations = meta.get("deviations") or []
            deviations.append({"action": "step_cancelled", "step_id": entity_id,
                               "reason": reason, "at": int(time.time())})
            update_metadata("task", str(entity["task_id"]), {"deviations": deviations})
    return {"ok": True}

@mcp.tool(name="todo.mint_token")
def mint_token_tool(token_cls: str, binding: Optional[Dict[str, Any]] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    if token_cls not in ("worker", "step_owner", "validator", "executor"):
        raise ValueError("invalid class")
    # 2026-11-20 (Muse, at madchaz's direction): worker TTL 15 min -> 4 h.
    # A worker token must span the whole lifecycle of its work order,
    # including the accept/validate wait; 15 min expired mid-closing
    # (breakpoint e78fbac8). The token stays bound to exactly one work order.
    ttl = 4*60*60 if token_cls == "worker" else 60*60
    token = mint_token(token_cls, binding or {}, ttl)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    # 2026-11-22 batch 4: return the expiry so callers can reason about
    # token lifetime from the response instead of the skill's TTL table.
    # Decoded from the token's own payload - the payload is the source.
    try:
        payload_b64 = token.split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        expires_at = int(payload["exp"])
    except Exception:
        expires_at = int(time.time()) + ttl
    return {"token": token, "token_hash": token_hash,
            "token_cls": token_cls, "expires_at": expires_at}

# --- Tools: reading -----------------------------------------------------

@mcp.tool(name="todo.list_projects")
def list_projects():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status FROM todo_projects ORDER BY created_at")
            return cur.fetchall()

@mcp.tool(name="todo.list_tasks")
def list_tasks(project_id: Optional[str] = None):
    """Tasks are workflow INSTANCES (one run of a workflow, or an ad-hoc run)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if project_id:
                cur.execute("SELECT id, project_id, name, status, template_id, template_version, revision, depends_on FROM todo_tasks WHERE project_id=%s ORDER BY created_at", (project_id,))
            else:
                cur.execute("SELECT id, project_id, name, status, template_id, template_version, revision, depends_on FROM todo_tasks ORDER BY created_at")
            rows = cur.fetchall()
            for r in rows:
                r["depends_on"] = _deps_of(r)
            return rows

def _deps_of(row):
    """Normalize a row's depends_on to a list of id strings. The column
    is jsonb: a list of ids, or '{}' for rows never given dependencies.
    Read verbs surface it (2026-11-22 fix, Muse at madchaz's
    direction): the orchestrator could not see step dependencies in
    any read output and inferred them from list order, defaulting to
    serial dispatch. The store held the fact; the reads hid it."""
    deps = (row or {}).get("depends_on") or []
    if isinstance(deps, dict):
        return []
    return [str(d) for d in deps]


def _meta_parse(row):
    """Parse a row's jsonb metadata to a dict ({} when absent)."""
    m = (row or {}).get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = {}
    return m if isinstance(m, dict) else {}


def _lift_wo(row):
    """Surface a work order's decision-relevant metadata as fields
    (2026-11-22 read-richness fix): accepted_by and the executor's
    probe records were stored only inside metadata, which no read
    verb returned - acceptance was invisible and the probe flow
    could only be checked by attempting validation and reading the
    guard's refusal. The full metadata stays available through
    todo.get_work_order."""
    meta = _meta_parse(row)
    row["accepted_by"] = meta.get("accepted_by")
    row["probe_result"] = meta.get("probe_result")
    row["probe_confirm"] = meta.get("probe_confirm")
    row.pop("metadata", None)
    return row


@mcp.tool(name="todo.list_steps")
def list_steps(task_id: Optional[str] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if task_id:
                cur.execute("SELECT id, task_id, name, status, revision, depends_on, order_idx, metadata FROM todo_steps WHERE task_id=%s ORDER BY order_idx, created_at", (task_id,))
            else:
                cur.execute("SELECT id, task_id, name, status, revision, depends_on, order_idx, metadata FROM todo_steps ORDER BY order_idx, created_at")
            rows = cur.fetchall()
            for r in rows:
                r["depends_on"] = _deps_of(r)
                _m = _meta_parse(r)
                r["agent"] = _m.get("agent")
                r["key"] = _m.get("key")
                r.pop("metadata", None)
            return rows

@mcp.tool(name="todo.list_work_orders")
def list_work_orders(step_id: Optional[str] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if step_id:
                cur.execute("SELECT id, step_id, name, status, revision, assigned_worker, artifact_path, subagent_session_id, metadata FROM todo_work_orders WHERE step_id=%s ORDER BY created_at", (step_id,))
            else:
                cur.execute("SELECT id, step_id, name, status, revision, assigned_worker, artifact_path, subagent_session_id, metadata FROM todo_work_orders ORDER BY created_at")
            rows = cur.fetchall()
            for r in rows:
                _lift_wo(r)
            return rows

@mcp.tool(name="todo.get_work_order")
def get_work_order(work_order_id: str):
    """Full detail for one work order: every column, metadata included
    (accepted_by, the structured completion, probe_result /
    probe_confirm, guard notes). The list reads stay lean; this is
    the detail view (2026-11-22)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_work_orders WHERE id=%s", (work_order_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"work order not found: {work_order_id}")
            row["metadata"] = _meta_parse(row)
            return row

@mcp.tool(name="todo.get_task")
def get_task(task_id: str):
    """Full detail for one task: every column, metadata parsed (the
    template stamp and recorded deviations live there), depends_on
    normalized. Symmetry with todo.get_work_order (2026-11-22 batch 4)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"task not found: {task_id}")
            row["metadata"] = _meta_parse(row)
            row["depends_on"] = _deps_of(row)
            return row

@mcp.tool(name="todo.get_step")
def get_step(step_id: str):
    """Full detail for one step: every column, metadata parsed (owner,
    template key), depends_on normalized. Symmetry with
    todo.get_work_order (2026-11-22 batch 4)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_steps WHERE id=%s", (step_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"step not found: {step_id}")
            row["metadata"] = _meta_parse(row)
            row["depends_on"] = _deps_of(row)
            return row

@mcp.tool(name="todo.list_comments")
def list_comments(entity_type: str, entity_id: str, limit: Optional[int] = None):
    if entity_type not in COMMENTABLE_ENTITY_TYPES:
        raise ValueError("invalid entity type")
    with get_conn() as conn:
        with conn.cursor() as cur:
            if limit:
                cur.execute("SELECT id, author, body, created_at FROM (SELECT id, author, body, created_at FROM todo_comments WHERE entity_type=%s AND entity_id=%s ORDER BY created_at DESC LIMIT %s) recent ORDER BY created_at", (entity_type, entity_id, int(limit)))
            else:
                cur.execute("SELECT id, author, body, created_at FROM todo_comments WHERE entity_type=%s AND entity_id=%s ORDER BY created_at", (entity_type, entity_id))
            return cur.fetchall()

@mcp.tool(name="todo.list_events")
def list_events(entity_type: str, entity_id: str):
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise ValueError("invalid entity type")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, from_status, to_status, actor_class, actor_id, timestamp FROM todo_transition_events WHERE entity_type=%s AND entity_id=%s ORDER BY timestamp", (entity_type, entity_id))
            return cur.fetchall()

@mcp.tool(name="todo.agent_load")
def agent_load():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT assigned_worker, open_tasks, in_progress_tasks, done_proposed_tasks, done_tasks FROM todo_agent_load")
            return cur.fetchall()

@mcp.tool(name="todo.board_view")
def board_view(project_id: Optional[str] = None):
    # 2026-11-19 fix (Muse, at madchaz's direction): the previous version
    # called derived_state() for every node; derived_state/rollup_label
    # recurse over the whole subtree per node and every helper opens a fresh
    # database connection - tens of thousands of connections per call over a
    # ~250-row board, which wedged this server while Postgres sat idle. The
    # board is now computed in memory from the four result sets below, with
    # identical derived/rollup semantics, memoized per entity. Four queries,
    # one connection, no recursion into the database.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status FROM todo_projects ORDER BY created_at")
            projects_raw = cur.fetchall()
            cur.execute("SELECT id, project_id, name, status, template_id, template_version, depends_on FROM todo_tasks ORDER BY created_at")
            tasks_raw = cur.fetchall()
            cur.execute("SELECT id, task_id, name, status, order_idx, depends_on, metadata FROM todo_steps ORDER BY order_idx, created_at")
            steps_raw = cur.fetchall()
            cur.execute("SELECT id, step_id, name, status, revision, assigned_worker, artifact_path, subagent_session_id, metadata FROM todo_work_orders ORDER BY created_at")
            orders_raw = cur.fetchall()

    by_id = {
        "project": {str(r["id"]): r for r in projects_raw},
        "task": {str(r["id"]): r for r in tasks_raw},
        "step": {str(r["id"]): r for r in steps_raw},
        "work_order": {str(r["id"]): r for r in orders_raw},
    }
    tasks_by_proj = {}
    for t in tasks_raw:
        tasks_by_proj.setdefault(str(t["project_id"]), []).append(t)
    steps_by_task = {}
    for s in steps_raw:
        steps_by_task.setdefault(str(s["task_id"]), []).append(s)
    orders_by_step = {}
    for o in orders_raw:
        orders_by_step.setdefault(str(o["step_id"]), []).append(o)

    def children_of(entity_type, eid):
        if entity_type == "project":
            return tasks_by_proj.get(eid, [])
        if entity_type == "task":
            return steps_by_task.get(eid, [])
        if entity_type == "step":
            return orders_by_step.get(eid, [])
        return []

    memo = {}

    def compute(entity_type, row):
        eid = str(row["id"])
        key = (entity_type, eid)
        if key in memo:
            return memo[key]
        children = children_of(entity_type, eid)
        if not children:
            rollup = None
        else:
            ct = child_type_for(entity_type)
            rolls = []
            for c in children:
                cds = compute(ct, c)
                rolls.append(cds["rollup"] if cds["rollup"] is not None else c["status"])
            normed = [str(r).lower() for r in rolls]
            if all(r == "done" for r in normed):
                rollup = "done"
            elif any(r in ("in_progress", "done_proposed", "in progress") for r in normed):
                rollup = "In progress"
            elif all(r == "open" for r in normed):
                rollup = "open"
            else:
                rollup = "In progress"
        status = row["status"]
        if status != "open":
            derived = status
        else:
            deps = row.get("depends_on") or []
            dep_rows = [by_id[entity_type].get(str(d)) for d in deps]
            if any(dr is not None and dr["status"] == "cancelled" for dr in dep_rows):
                derived = "blocked"
            elif not deps or (len({str(d) for d, dr in zip(deps, dep_rows) if dr is not None}) == len(deps)
                              and all(dr["status"] == "done" for dr in dep_rows if dr is not None)):
                derived = "ready"
            else:
                derived = "waiting"
        out = {"derived": derived, "rollup": rollup}
        memo[key] = out
        return out

    def build_node(entity_type, row):
        ds = compute(entity_type, row)
        node = {
            "id": str(row["id"]),
            "name": row["name"],
            "status": row["status"],
            "derived": ds["derived"],
            "rollup": ds["rollup"],
        }
        if entity_type in ("task", "step"):
            node["depends_on"] = _deps_of(row)
        if entity_type == "step":
            node["order_idx"] = row.get("order_idx")
        if entity_type == "work_order":
            node["assigned_worker"] = row.get("assigned_worker")
            node["artifact_path"] = row.get("artifact_path")
            node["subagent_session_id"] = (str(row["subagent_session_id"]) if row.get("subagent_session_id") else None)
            _lift_wo(row)
            node["accepted_by"] = row.get("accepted_by")
            node["probe_result"] = row.get("probe_result")
            node["probe_confirm"] = row.get("probe_confirm")
        return node

    projects = []
    for p in projects_raw:
        pid = str(p["id"])
        proj_node = build_node("project", p)
        tlist = []
        for t in tasks_by_proj.get(pid, []):
            tnode = build_node("task", t)
            if t.get("template_id"):
                tnode["template_id"] = str(t["template_id"])
                tnode["template_version"] = t.get("template_version")
            steps = []
            for s in steps_by_task.get(str(t["id"]), []):
                snode = build_node("step", s)
                meta = s.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                snode["agent"] = meta.get("agent")
                snode["key"] = meta.get("key")
                orders = []
                for o in orders_by_step.get(str(s["id"]), []):
                    orders.append(build_node("work_order", o))
                snode["work_orders"] = orders
                steps.append(snode)
            tnode["steps"] = steps
            tlist.append(tnode)
        proj_node["tasks"] = tlist
        projects.append(proj_node)

    if project_id:
        projects = [p for p in projects if p["id"] == str(project_id)]
    return {"projects": projects}

# --- WS4 guards (activated 2026-11-22) ------------------------------------
# Deny-only, per the WS1 framework. Each guard checks a mechanical
# predicate; adequacy judgments stay with the validator (plan C14).

def _meta_of(entity):
    m = (entity or {}).get("metadata") or {}
    if isinstance(m, str):
        m = json.loads(m)
    return m

def guard_probe_validate(entity_type, entity_id, ctx):
    """A completion that declares a probe may not validate until the
    executor has graded THAT completion (verdict) and re-graded it
    after acceptance with an identical digest (confirm). The pair is
    the before/after snapshot comparison: an artifact swapped after
    acceptance changes the confirm digest and refuses the closure."""
    if entity_type != "work_order":
        return
    meta = _meta_of(get_entity("work_order", entity_id))
    comp = meta.get("completion") or {}
    if not comp.get("probe_id"):
        return
    pr = meta.get("probe_result") or {}
    if pr.get("probe_id") != comp["probe_id"] or pr.get("completion_at") != comp.get("at"):
        raise GuardDenied("probe",
                          "no executor verdict recorded for this completion yet",
                          {"probe_id": comp["probe_id"]})
    pc = meta.get("probe_confirm") or {}
    if pc.get("completion_at") != comp.get("at"):
        raise GuardDenied("probe",
                          "post-acceptance confirmation probe not yet recorded",
                          {"probe_id": comp["probe_id"]})
    if pc.get("digest") != pr.get("digest"):
        raise GuardDenied("probe",
                          "confirmation digest differs from verdict digest - the artifact changed after acceptance",
                          {"probe_id": comp["probe_id"], "verdict": pr.get("digest"), "confirm": pc.get("digest")})

def guard_audit_validate(entity_type, entity_id, ctx):
    """In an audit_required task, no step validates done before the
    audit step itself - implement work waits in done_proposed while
    the audit runs against it (the C2 choreography, AGENTS.md 3.6)."""
    if entity_type != "step":
        return
    step = get_entity("step", entity_id)
    if not step:
        return
    task = get_entity("task", str(step["task_id"]))
    if not _meta_of(task).get("audit_required"):
        return
    if _meta_of(step).get("key") == "audit":
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, metadata FROM todo_steps WHERE task_id=%s", (str(step["task_id"]),))
            audit = [r for r in cur.fetchall() if _meta_of(r).get("key") == "audit"]
    if any(r["status"] == "done" for r in audit):
        return
    if audit and all(r["status"] == "cancelled" for r in audit):
        raise GuardDenied("audit",
                          "the audit step is cancelled and no audit step has validated; create a replacement step with key 'audit' (todo.create_step) and complete it - this task cannot validate until an audit step is done",
                          {"task_id": str(step["task_id"])})
    raise GuardDenied("audit",
                      "audit step is not validated done yet; this step waits in done_proposed",
                      {"task_id": str(step["task_id"])})

def _active_descendants(entity_type, entity_id):
    active = []
    def walk(et, eid, depth):
        for c in get_children(et, eid):
            if c["status"] in ("in_progress", "done_proposed"):
                active.append((child_type_for(et), str(c["id"]), c["status"]))
            if depth < 3:
                walk(child_type_for(et), str(c["id"]), depth + 1)
    walk(entity_type, entity_id, 0)
    return active

def guard_cancel_destructive(entity_type, entity_id, ctx):
    """Cancelling an element with live work under it is refused unless
    the caller passes force=true (recorded). Parked debris has no
    active descendants and cancels freely, as before."""
    if ctx.get("force"):
        return
    active = _active_descendants(entity_type, entity_id)
    if active:
        raise GuardDenied("destructive",
                          f"element has {len(active)} active descendant(s) (in_progress/done_proposed); cancel them first or pass force=true",
                          {"active": active[:10]})

def guard_template_version_guard(entity_type, entity_id, ctx):
    """A new template version may not DROP a step key while live
    (non-terminal) instances of the template exist - their gates
    would lose meaning mid-flight."""
    tpl_row = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM todo_workflow_templates WHERE name=%s", (ctx.get("name"),))
            tpl_row = cur.fetchone()
    if not tpl_row:
        return
    latest = get_template_version(str(tpl_row["id"]))
    if not latest:
        return
    dag = latest["step_dag"]
    if isinstance(dag, str):
        dag = json.loads(dag)
    prev_keys = {s.get("key") for s in dag}
    dropped = prev_keys - set(ctx.get("new_keys") or [])
    if not dropped:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM todo_tasks WHERE template_id=%s AND status NOT IN ('done','cancelled')",
                        (str(tpl_row["id"]),))
            live = cur.fetchone()["n"]
    if live:
        raise GuardDenied("destructive",
                          f"new version drops step key(s) {sorted(dropped)} while {live} live instance(s) of this template exist",
                          {"dropped": sorted(dropped), "live_instances": live})

def guard_cancel_audit_step(entity_type, entity_id, ctx):
    """Cancelling the audit step of an audit_required task while sibling
    steps are live would deadlock the task: the audit guard requires an
    audit step done, and a cancelled step can never be done (third
    review, 2026-11-22). Refused unless force=true (recorded, as with
    other destructive acts). Recovery exists regardless: a replacement
    step can be created with todo.create_step(key='audit')."""
    if entity_type != "step" or ctx.get("force"):
        return
    step = get_entity("step", entity_id)
    if not step or _meta_of(step).get("key") != "audit":
        return
    task = get_entity("task", str(step["task_id"]))
    if not task or not _meta_of(task).get("audit_required"):
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM todo_steps WHERE task_id=%s AND id<>%s AND status NOT IN ('done','cancelled')",
                        (str(step["task_id"]), entity_id))
            live = cur.fetchone()["n"]
    if live:
        raise GuardDenied("destructive",
                          f"this is the audit step of an audit_required task with {live} live sibling step(s); cancelling it deadlocks the task's validations - complete or cancel the siblings first, or pass force=true",
                          {"task_id": str(step["task_id"]), "live_siblings": live})

GUARDS["validate_done"].extend([guard_probe_validate, guard_audit_validate])
GUARDS["cancel_element"].append(guard_cancel_destructive)
GUARDS["cancel_element"].append(guard_cancel_audit_step)
GUARDS["add_template_version"].append(guard_template_version_guard)

# --- Tools: work-order lifecycle ----------------------------------------
# A worker token binds to ONE work order: binding {"work_order_id": "<id>"}.
# A step_owner token binds to an agent identity: binding {"agent": "<name>"}
# and may act only on steps whose metadata.agent equals that identity.

def _worker_bound_work_order(auth, work_order_id: str):
    if not auth or auth.get("cls") != "worker":
        raise PermissionError("worker required")
    binding = auth.get("binding", {})
    if binding.get("work_order_id") != work_order_id:
        raise PermissionError("worker bound to different work order")

def _step_owner_of(auth, step_id: str):
    if not auth or auth.get("cls") != "step_owner":
        raise PermissionError("step_owner required")
    step = get_entity("step", step_id)
    if not step:
        raise ValueError("step not found")
    meta = step.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    agent = meta.get("agent")
    token_agent = auth.get("binding", {}).get("agent")
    if agent != token_agent:
        raise PermissionError("step not owned by this agent")
    return token_agent

@mcp.tool(name="todo.start_work_order")
@call_token
def start_work_order(work_order_id: str, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    _worker_bound_work_order(auth, work_order_id)
    order = get_entity("work_order", work_order_id)
    step_id = str(order["step_id"])
    if not dependencies_done("step", step_id):
        raise ValueError("step dependencies not done")
    check_status("work_order", work_order_id, "open")
    check_revision("work_order", work_order_id, expected_revision)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM todo_steps WHERE id=%s", (step_id,))
            step_row = cur.fetchone()
            if step_row["status"] == "open":
                cur.execute("UPDATE todo_steps SET status='in_progress', revision=revision+1, updated_at=now() WHERE id=%s", (step_id,))
                write_transition(conn, "step", step_id, "open", "in_progress", "worker")
            cur.execute("UPDATE todo_work_orders SET status='in_progress', revision=revision+1, updated_at=now() WHERE id=%s", (work_order_id,))
            write_transition(conn, "work_order", work_order_id, "open", "in_progress", "worker")
    return {"ok": True}

ARTIFACT_KINDS = {"code", "document", "config", "service", "data", "none"}

@mcp.tool(name="todo.propose_done")
@call_token
def propose_done(work_order_id: str, comment: Optional[str] = None,
                 artifact_kind: Optional[str] = None, deployed_location: Optional[str] = None,
                 probe_id: Optional[str] = None, background_work: Optional[List[str]] = None,
                 expected_revision: Optional[int] = None, probe: Optional[Dict[str, Any]] = None, token: Optional[str] = None):
    # WS1 (2026-11-21): the structured return is the ONLY shape. A worker
    # states what kind of artifact it produced, where the change is
    # deployed or running, which acceptance probe covers it (probe_id,
    # nullable when no probe applies), and any background work it left
    # running (background_work, a list, possibly empty). Unstructured
    # proposals are refused: claims the validator cannot check are not
    # completions.
    auth = get_auth()
    _worker_bound_work_order(auth, work_order_id)
    check_status("work_order", work_order_id, "in_progress")
    check_revision("work_order", work_order_id, expected_revision)
    missing = []
    if artifact_kind not in ARTIFACT_KINDS:
        missing.append("artifact_kind (one of code/document/config/service/data/none)")
    if not deployed_location:
        missing.append("deployed_location (where the change runs or lives; 'not deployed' is a value, absence is not)")
    if background_work is None:
        missing.append("background_work (list of still-running work you started, or [] )")
    if missing:
        raise ValueError("structured propose_done requires: " + "; ".join(missing))
    completion = {"artifact_kind": artifact_kind, "deployed_location": deployed_location,
                  "probe_id": probe_id, "background_work": background_work,
                  "at": int(time.time())}
    if probe is not None:
        # WS3: the acceptance probe SPEC (authored at plan time, judged by
        # the validator) rides the completion; the VM-local executor runs
        # it and records the verdict via todo.record_probe.
        completion["probe"] = probe
        if not probe_id and isinstance(probe, dict):
            completion["probe_id"] = probe.get("id")
    guarded("propose_done", "work_order", work_order_id, {"completion": completion})
    update_metadata("work_order", work_order_id, {"completion": completion})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_work_orders SET status='done_proposed', revision=revision+1, updated_at=now() WHERE id=%s", (work_order_id,))
            write_transition(conn, "work_order", work_order_id, "in_progress", "done_proposed", "worker")
            # 2026-11-20 (Muse, at madchaz's direction): the closing comment
            # rides propose_done. A post-validation append_comment depends on
            # the worker token still being alive at closing time; validation
            # waits outlast it (breakpoint e78fbac8: token dead ~05:36 EST,
            # validation landed 06:28 EST, closing append refused). Writing
            # the comment here, atomically with the transition, removes the
            # closing-comment errand class entirely.
            if comment:
                cur.execute("INSERT INTO todo_comments (entity_type, entity_id, author, body) VALUES (%s,%s,%s,%s)",
                            ("work_order", work_order_id, "worker", comment))
    return {"ok": True, "completion": completion}

@mcp.tool(name="todo.set_artifact_path")
@call_token
def set_artifact_path(work_order_id: str, artifact_path: str, token: Optional[str] = None):
    auth = get_auth()
    _worker_bound_work_order(auth, work_order_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_work_orders SET artifact_path=%s, updated_at=now() WHERE id=%s", (artifact_path, work_order_id))
    return {"ok": True}

@mcp.tool(name="todo.append_comment")
@call_token
def append_comment(entity_type: str, entity_id: str, body: str, token: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "worker":
        raise PermissionError("worker required")
    if entity_type != "work_order":
        raise PermissionError("worker can only comment on work orders")
    _worker_bound_work_order(auth, entity_id)
    insert_comment(entity_type, entity_id, "worker", body)
    return {"ok": True}

@mcp.tool(name="todo.create_work_order")
@call_token
def create_work_order(step_id: str, name: str, assigned_worker: Optional[str] = None, token: Optional[str] = None):
    auth = get_auth()
    _step_owner_of(auth, step_id)
    guarded("create_work_order", "step", step_id, {"name": name})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO todo_work_orders (step_id, name, status, assigned_worker) VALUES (%s,%s,'open',%s) RETURNING id", (step_id, name, assigned_worker))
            wid = cur.fetchone()["id"]
    return {"id": str(wid)}

@mcp.tool(name="todo.accept_work_order")
@call_token
def accept_work_order(work_order_id: str, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    order = get_entity("work_order", work_order_id)
    token_agent = _step_owner_of(auth, str(order["step_id"]))
    check_status("work_order", work_order_id, "done_proposed")
    check_revision("work_order", work_order_id, expected_revision)
    update_metadata("work_order", work_order_id, {"accepted_by": token_agent, "accepted_at": int(time.time())})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_work_orders SET revision=revision+1, updated_at=now() WHERE id=%s", (work_order_id,))
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_transition(conn, "work_order", work_order_id, "done_proposed", "done_proposed", "step_owner", token_agent)
    return {"ok": True}

@mcp.tool(name="todo.reject_work_order")
@call_token
def reject_work_order(work_order_id: str, reason: str, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    order = get_entity("work_order", work_order_id)
    _step_owner_of(auth, str(order["step_id"]))
    check_status("work_order", work_order_id, "done_proposed")
    check_revision("work_order", work_order_id, expected_revision)
    insert_comment("work_order", work_order_id, "step_owner", f"rejected: {reason}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_work_orders SET status='in_progress', revision=revision+1, updated_at=now() WHERE id=%s", (work_order_id,))
            write_transition(conn, "work_order", work_order_id, "done_proposed", "in_progress", "step_owner")
    return {"ok": True}

@mcp.tool(name="todo.cancel_work_order")
@call_token
def cancel_work_order(work_order_id: str, reason: str, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    order = get_entity("work_order", work_order_id)
    _step_owner_of(auth, str(order["step_id"]))
    if order["status"] in ("done", "cancelled"):
        raise ValueError(f"cannot cancel work order in status {order['status']}")
    check_revision("work_order", work_order_id, expected_revision)
    from_status = order["status"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_work_orders SET status='cancelled', revision=revision+1, updated_at=now() WHERE id=%s", (work_order_id,))
            write_transition(conn, "work_order", work_order_id, from_status, "cancelled", "step_owner")
    insert_comment("work_order", work_order_id, "step_owner", f"cancelled: {reason}")
    return {"ok": True}

def _next_ready():
    """The board's next dispatchable work, computed as a closure lands
    (batch 8, 2026-11-22): the oldest open work order passing the same
    gate request_slot applies - step dependencies done, no active grant -
    under a live step/task/project, with the currently free slot nodes.
    Closure verbs carry it in their response so the next action is in
    front of the orchestrator at the moment she would otherwise end the
    turn on a summary. None when nothing is dispatch-ready.

    Batch 9 (2026-11-22): when no work order is dispatch-ready, fall
    back to the oldest ready STEP holding no live work order - step
    open/in_progress, its own dependencies done, task/project live, no
    order in open/in_progress/done_proposed under it. The next act then
    is creating that step's work order, the one act batch 8 could not
    name (the 2026-11-22 researcher execute step stood ready with no
    order under it while the turn ended on a summary). The response
    discriminates on "kind": "work_order" (batch 8 shape plus kind) or
    "step" with action "create_work_order". ready_count counts items
    of the returned kind."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            _expire_held(cur)
            cur.execute("SELECT node FROM todo_slot_grants WHERE state IN ('held','bound')")
            busy = {r["node"] for r in cur.fetchall()}
            cur.execute("""
                SELECT w.id AS wo_id, w.name AS wo_name, w.step_id AS step_id,
                       s.name AS step_name, t.name AS task_name, p.name AS project_name
                FROM todo_work_orders w
                JOIN todo_steps s ON s.id = w.step_id
                JOIN todo_tasks t ON t.id = s.task_id
                JOIN todo_projects p ON p.id = t.project_id
                WHERE w.status = 'open'
                  AND s.status NOT IN ('done','cancelled')
                  AND t.status NOT IN ('done','cancelled')
                  AND p.status NOT IN ('done','cancelled')
                  AND NOT EXISTS (SELECT 1 FROM todo_slot_grants g
                                  WHERE g.work_order_id = w.id AND g.state IN ('held','bound'))
                ORDER BY w.created_at ASC""")
            candidates = cur.fetchall()
            free_nodes = [n for n in SLOT_NODES if n not in busy]
    ready = [r for r in candidates if dependencies_done("step", str(r["step_id"]))]
    if ready:
        first = ready[0]
        return {"kind": "work_order",
                "work_order_id": str(first["wo_id"]), "name": first["wo_name"],
                "step_name": first["step_name"], "task_name": first["task_name"],
                "project_name": first["project_name"],
                "ready_count": len(ready),
                "free_nodes": free_nodes}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id AS step_id, s.name AS step_name,
                       s.metadata->>'agent' AS step_owner,
                       t.name AS task_name, p.name AS project_name
                FROM todo_steps s
                JOIN todo_tasks t ON t.id = s.task_id
                JOIN todo_projects p ON p.id = t.project_id
                WHERE s.status IN ('open','in_progress')
                  AND t.status NOT IN ('done','cancelled')
                  AND p.status NOT IN ('done','cancelled')
                  AND NOT EXISTS (SELECT 1 FROM todo_work_orders w
                                  WHERE w.step_id = s.id
                                    AND w.status IN ('open','in_progress','done_proposed'))
                ORDER BY s.created_at ASC""")
            step_candidates = cur.fetchall()
    ready_steps = [r for r in step_candidates if dependencies_done("step", str(r["step_id"]))]
    if not ready_steps:
        return None
    first = ready_steps[0]
    return {"kind": "step", "action": "create_work_order",
            "step_id": str(first["step_id"]), "step_name": first["step_name"],
            "step_owner": first["step_owner"],
            "task_name": first["task_name"], "project_name": first["project_name"],
            "ready_count": len(ready_steps),
            "free_nodes": free_nodes}

@mcp.tool(name="todo.propose_step_done")
@call_token
def propose_step_done(step_id: str, reason: Optional[str] = None, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    _step_owner_of(auth, step_id)
    step = get_entity("step", step_id)
    check_revision("step", step_id, expected_revision)
    guarded("propose_step_done", "step", step_id, {"reason": reason})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM todo_work_orders WHERE step_id=%s", (step_id,))
            orders = cur.fetchall()
            if not orders:
                # A step whose work predates work-order discipline can still be
                # closed, but only with the reason recorded on the step itself.
                if not reason:
                    raise ValueError("step has no work orders; a closure reason is required")
                if step["status"] not in ("open", "in_progress"):
                    raise ValueError(f"cannot propose done from status {step['status']}")
                cur.execute("INSERT INTO todo_comments (entity_type, entity_id, author, body) VALUES (%s,%s,%s,%s)",
                            ("step", step_id, "step_owner", f"closure proposed without work orders: {reason}"))
                cur.execute("UPDATE todo_steps SET status='done_proposed', revision=revision+1, updated_at=now() WHERE id=%s", (step_id,))
                write_transition(conn, "step", step_id, step["status"], "done_proposed", "step_owner")
                return {"ok": True, "next_ready": _next_ready()}
            for o in orders:
                if o["status"] not in ("done", "cancelled"):
                    raise ValueError("not all work orders done or cancelled")
    check_status("step", step_id, "in_progress")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_steps SET status='done_proposed', revision=revision+1, updated_at=now() WHERE id=%s", (step_id,))
            write_transition(conn, "step", step_id, "in_progress", "done_proposed", "step_owner")
    return {"ok": True, "next_ready": _next_ready()}


@mcp.tool(name="todo.set_step_owner")
@call_token
def set_step_owner(step_id: str, agent: str, token: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    if agent not in ALLOWED_AGENTS:
        raise ValueError(f"agent must be one of {sorted(ALLOWED_AGENTS)}")
    step = get_entity("step", step_id)
    if not step:
        raise ValueError("step not found")
    meta = step.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    previous = meta.get("agent")
    update_metadata("step", step_id, {"agent": agent})
    insert_comment("step", step_id, "orchestrator",
                   f"step owner set to {agent} by orchestrator (was: {previous}); owner repair")
    return {"ok": True, "previous": previous, "agent": agent}

@mcp.tool(name="todo.propose_done_element")
def propose_done_element(entity_type: str, entity_id: str, expected_revision: Optional[int] = None):
    """Propose a TASK or PROJECT done (orchestrator). Every child must be done
    or cancelled; the validator still makes the final move via validate_done.
    The orchestrator proposes - she never marks done."""
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    if entity_type not in ("task", "project"):
        raise ValueError("propose_done_element covers task and project; steps use propose_step_done")
    entity = get_entity(entity_type, entity_id)
    if not entity:
        raise ValueError("entity not found")
    if entity["status"] not in ("open", "in_progress"):
        raise ValueError(f"cannot propose done from status {entity['status']}")
    children = get_children(entity_type, entity_id)
    if not children:
        raise ValueError("element has no children; cancellation is the honest close for an empty element")
    for c in children:
        if c["status"] not in ("done", "cancelled"):
            raise ValueError("not all children done or cancelled")
    check_revision(entity_type, entity_id, expected_revision)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {entity_table(entity_type)} SET status='done_proposed', revision=revision+1, updated_at=now() WHERE id=%s", (entity_id,))
            write_transition(conn, entity_type, entity_id, entity["status"], "done_proposed", "orchestrator")
    return {"ok": True, "next_ready": _next_ready()}

@mcp.tool(name="todo.validate_done")
@call_token
def validate_done(entity_type: str, entity_id: str, expected_revision: Optional[int] = None, token: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") != "validator":
        raise PermissionError("validator required")
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise ValueError("invalid entity type")
    entity = get_entity(entity_type, entity_id)
    if not entity or entity["status"] != "done_proposed":
        raise ValueError("not proposed")
    check_revision(entity_type, entity_id, expected_revision)
    guarded("validate_done", entity_type, entity_id, {})
    if entity_type == "work_order":
        meta = entity.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        if not meta.get("accepted_by"):
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE todo_work_orders SET status='in_progress', revision=revision+1, updated_at=now() WHERE id=%s", (entity_id,))
                    write_transition(conn, "work_order", entity_id, "done_proposed", "in_progress", "validator")
                    cur.execute("INSERT INTO todo_comments (entity_type, entity_id, author, body) VALUES (%s,%s,%s,%s)",
                                ("work_order", entity_id, "validator", "validation failed: acceptance not recorded"))
            raise ValueError("validation failed: acceptance not recorded")
        if not entity.get("artifact_path"):
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE todo_work_orders SET status='in_progress', revision=revision+1, updated_at=now() WHERE id=%s", (entity_id,))
                    write_transition(conn, "work_order", entity_id, "done_proposed", "in_progress", "validator")
                    cur.execute("INSERT INTO todo_comments (entity_type, entity_id, author, body) VALUES (%s,%s,%s,%s)",
                                ("work_order", entity_id, "validator", "validation failed: artifact missing"))
            raise ValueError("validation failed: artifact missing")
    if entity_type == "step":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM todo_work_orders WHERE step_id=%s", (entity_id,))
                orders = cur.fetchall()
                if not all(o["status"] in ("done", "cancelled") for o in orders):
                    raise ValueError("step validation failed: not all work orders done or cancelled")
    if not dependencies_done(entity_type, entity_id):
        raise ValueError("dependencies not done")
    with get_conn() as conn:
        with conn.cursor() as cur:
            table = entity_table(entity_type)
            cur.execute(f"UPDATE {table} SET status='done', revision=revision+1, updated_at=now() WHERE id=%s", (entity_id,))
            write_transition(conn, entity_type, entity_id, "done_proposed", "done", "validator")
    return {"ok": True, "next_ready": _next_ready()}

# --- Slot grants (WS2, 2026-11-22) ---------------------------------------
# The dispatch cap (AGENTS.md 4.2/4.2b: one running worker per node) as a
# mechanism. The ledger is the placement source of truth; the VM-local
# reconciler keeps it honest against the session store. A grant is
# requested BEFORE a dispatch or a resume, bound to the child session
# immediately after spawning, and released by the reconciler when the
# session ends or goes quiet (or explicitly via release_slot).
SLOT_NODES = ["db01", "db03", "db04"]
SLOT_PROVIDERS = {"db01": "glimmer-db01", "db03": "glimmer-db03", "db04": "glimmer-db04"}
HELD_TTL_SECONDS = 600

def _expire_held(cur):
    cur.execute("UPDATE todo_slot_grants SET state='expired', released_at=now(), release_reason='held TTL expired unbound' WHERE state='held' AND expires_at IS NOT NULL AND expires_at < now()")

def _grant_row(r):
    if r is None:
        return None
    out = dict(r)
    for k in ("id", "work_order_id"):
        out[k] = str(out[k])
    for k in ("created_at", "expires_at", "bound_at", "released_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out

@mcp.tool(name="todo.request_slot")
def request_slot(work_order_id: str, mode: str = "dispatch", node: Optional[str] = None, session_id: Optional[str] = None):
    """Ask the tool for a dispatch slot (AGENTS.md 4.3: the tool grants
    the slot). Dispatch mode: the work order must be open, its step's
    dependencies done; returns the least-loaded free node in
    db01/db03/db04 order. Resume mode: the work order must be
    in_progress and node + session_id name the paused session's own
    node; the grant is issued only if that node is free."""
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    if mode not in ("dispatch", "resume"):
        raise ValueError("mode must be dispatch or resume")
    order = get_entity("work_order", work_order_id)
    if not order:
        raise ValueError("work order not found")
    if mode == "dispatch" and order["status"] != "open":
        raise ValueError(f"dispatch requires an open work order, got {order['status']}")
    if mode == "resume":
        if order["status"] != "in_progress":
            raise ValueError(f"resume requires an in_progress work order, got {order['status']}")
        if node not in SLOT_NODES:
            raise ValueError("resume requires the session's own node (db01/db03/db04)")
        if not session_id:
            raise ValueError("resume requires the paused session_id")
    if not dependencies_done("step", str(order["step_id"])):
        raise ValueError("step dependencies not done - no slot until they are")
    candidates = SLOT_NODES if mode == "dispatch" else [node]
    import psycopg as _pg
    with get_conn() as conn:
        with conn.cursor() as cur:
            _expire_held(cur)
            cur.execute("SELECT id FROM todo_slot_grants WHERE work_order_id=%s AND state IN ('held','bound')", (work_order_id,))
            if cur.fetchone():
                raise ValueError("work order already holds an active grant")
            for cand in candidates:
                try:
                    with conn.transaction():
                        cur.execute(
                            "INSERT INTO todo_slot_grants (work_order_id, node, session_id, mode, expires_at) VALUES (%s,%s,%s,%s, now() + make_interval(secs => %s)) RETURNING *",
                            (work_order_id, cand, session_id, mode, HELD_TTL_SECONDS))
                        row = cur.fetchone()
                        row = _grant_row(row)
                        return {"grant_id": row["id"], "grant": row, "node": cand,
                                "provider": SLOT_PROVIDERS[cand]}
                except _pg.errors.UniqueViolation:
                    continue
    return {"slot": None, "reason": ("node %s is occupied" % node) if mode == "resume" else "all nodes occupied"}

def _stamp_wo_session(cur, work_order_id, session_id):
    """Record the serving session on the work order itself (2026-11-22
    batch 3): subagent_session_id and assigned_at were defined on the
    table but written by no code path - the session link lived only in
    the grants ledger. bind_slot is the moment the fact becomes true,
    so it stamps both stores in the same transaction."""
    cur.execute("UPDATE todo_work_orders SET subagent_session_id=%s, assigned_at=now() WHERE id=%s",
                (session_id, work_order_id))


@mcp.tool(name="todo.bind_slot")
def bind_slot(grant_id: str, session_id: str):
    """Bind a held grant to the child session just spawned for it.
    Idempotent for the same session; refuses a different one."""
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            _expire_held(cur)
            cur.execute("SELECT * FROM todo_slot_grants WHERE id=%s", (grant_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("grant not found")
            if row["state"] == "bound" and row["session_id"] == session_id:
                _stamp_wo_session(cur, row["work_order_id"], session_id)
                return {"ok": True, "grant": _grant_row(row), "idempotent": True}
            if row["state"] != "held":
                raise ValueError(f"cannot bind a grant in state {row['state']}")
            cur.execute("UPDATE todo_slot_grants SET state='bound', session_id=%s, bound_at=now(), expires_at=NULL WHERE id=%s RETURNING *",
                        (session_id, grant_id))
            bound = cur.fetchone()
            _stamp_wo_session(cur, bound["work_order_id"], session_id)
            return {"ok": True, "grant": _grant_row(bound)}

@mcp.tool(name="todo.release_slot")
def release_slot(grant_id: str, reason: str):
    auth = get_auth()
    if not auth or auth.get("cls") != "orchestrator":
        raise PermissionError("orchestrator required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_slot_grants SET state='released', released_at=now(), release_reason=%s WHERE id=%s AND state IN ('held','bound') RETURNING *",
                        (reason, grant_id))
            row = cur.fetchone()
            if not row:
                raise ValueError("grant not found or not active")
            return {"ok": True, "grant": _grant_row(row)}

@mcp.tool(name="todo.list_grants")
def list_grants(state: Optional[str] = None):
    auth = get_auth()
    if not auth or auth.get("cls") not in ("orchestrator", "validator"):
        raise PermissionError("orchestrator or validator required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            if state:
                cur.execute("SELECT * FROM todo_slot_grants WHERE state=%s ORDER BY created_at DESC LIMIT 100", (state,))
            else:
                cur.execute("SELECT * FROM todo_slot_grants ORDER BY created_at DESC LIMIT 100")
            return [_grant_row(r) for r in cur.fetchall()]

@mcp.tool(name="todo.record_probe")
def record_probe(work_order_id: str, probe_id: Optional[str], digest: str,
                 value: Dict[str, Any], completion_at: int, phase: str = "verdict"):
    """Record an executor probe verdict on a work order (executor class
    only - the VM-local todo executor is the sole holder). The verdict is
    keyed to the completion it graded (completion_at), so a re-proposal
    invalidates older verdicts by construction."""
    auth = get_auth()
    if not auth or auth.get("cls") != "executor":
        raise PermissionError("executor required")
    order = get_entity("work_order", work_order_id)
    if not order:
        raise ValueError("work order not found")
    if phase not in ("verdict", "confirm"):
        raise ValueError("phase must be verdict or confirm")
    result = {"probe_id": probe_id, "digest": digest, "value": value,
              "completion_at": completion_at, "at": int(time.time()), "by": "executor"}
    key = "probe_result" if phase == "verdict" else "probe_confirm"
    update_metadata("work_order", work_order_id, {key: result})
    insert_comment("work_order", work_order_id, "executor",
                   f"probe {phase} [{probe_id}] digest {digest[:12]} for completion at {completion_at}")
    return {"ok": True}

@mcp.tool(name="todo.render_composition")
def render_composition(task_id: str):
    """Render a task instance's composition against its template: the
    template stamp (name, version, metadata, audit_required), each step's
    key/owner/status and whether it came from the template, and the
    recorded deviations. For ad-hoc tasks template fields are null."""
    task = get_entity("task", task_id)
    if not task:
        raise ValueError("task not found")
    meta = task.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status, order_idx, metadata FROM todo_steps WHERE task_id=%s ORDER BY order_idx", (task_id,))
            rows = cur.fetchall()
    steps = []
    for r in rows:
        sm = r.get("metadata") or {}
        if isinstance(sm, str):
            sm = json.loads(sm)
        steps.append({"id": str(r["id"]), "key": sm.get("key"), "name": r["name"],
                      "owner": sm.get("agent"), "status": r["status"],
                      "template_step": bool(sm.get("template_step"))})
    return {"task_id": str(task["id"]), "name": task["name"], "status": task["status"],
            "revision": task.get("revision"),
            "template": meta.get("template"), "template_version": meta.get("template_version"),
            "template_metadata": meta.get("template_metadata", {}),
            "audit_required": meta.get("audit_required", False),
            "steps": steps, "deviations": meta.get("deviations", [])}

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=TODO_PORT)
