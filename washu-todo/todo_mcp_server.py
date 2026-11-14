"""
washu-todo MCP server
FastMCP implementation with CRUD, assign, agent-load and markdown resources
"""
import os
import json
from typing import Optional
from fastmcp import FastMCP
import psycopg
from psycopg.rows import dict_row

DB_DSN = os.getenv("WASHU_POSTGRES_DSN")
BANK_ID = os.getenv("WASHU_BANK_ID")

mcp = FastMCP("washu-todo")

def get_conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row)

# ---------- Helpers ----------
def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# ---------- Tools ----------
@mcp.tool(name="todo.create_project")
def todo_create_project(name: str, description: Optional[str] = None, bank_id: Optional[str] = None, status: Optional[str] = None) -> dict:
    status = status or 'planned'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_projects (name, description, bank_id, status) VALUES (%s,%s,%s,%s) RETURNING id",
                (name, description, bank_id, status)
            )
            pid = cur.fetchone()["id"]
    return {"id": str(pid), "name": name, "status": status}

@mcp.tool(name="todo.create_workflow")
def todo_create_workflow(project_id: str, name: str, bank_id: Optional[str] = None, status: Optional[str] = None) -> dict:
    status = status or 'planned'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_workflows (project_id, name, bank_id, status) VALUES (%s,%s,%s,%s) RETURNING id",
                (project_id, name, bank_id, status)
            )
            wid = cur.fetchone()["id"]
    return {"id": str(wid), "project_id": project_id, "name": name, "status": status}

@mcp.tool(name="todo.create_step")
def todo_create_step(workflow_id: str, name: str, order_idx: Optional[int] = None, status: Optional[str] = None, depends_on: Optional[list] = None) -> dict:
    order_idx = order_idx if order_idx is not None else 0
    status = status or 'pending'
    depends_on = depends_on or []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO todo_steps (workflow_id, name, order_idx, status, depends_on) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (workflow_id, name, order_idx, status, depends_on)
            )
            sid = cur.fetchone()["id"]
    return {"id": str(sid), "workflow_id": workflow_id, "name": name, "status": status, "depends_on": depends_on}

@mcp.tool(name="todo.create_task")
def todo_create_task(step_id: str, name: str, status: Optional[str] = None, assigned_worker: Optional[str] = None, model_hint: Optional[str] = None, resource: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
    status = status or 'pending'
    metadata_json = json.dumps(metadata) if metadata else '{}'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO todo_tasks (step_id, name, status, assigned_worker, model_hint, resource, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (step_id, name, status, assigned_worker, model_hint, resource, metadata_json)
            )
            tid = cur.fetchone()["id"]
    return {"id": str(tid), "step_id": step_id, "name": name, "status": status, "metadata": metadata}

@mcp.tool(name="todo.update_status")
def todo_update_status(entity_type: str, entity_id: str, status: str) -> dict:
    query_map = {
        "project": "UPDATE todo_projects SET status=%s, updated_at=now() WHERE id=%s",
        "workflow": "UPDATE todo_workflows SET status=%s, updated_at=now() WHERE id=%s",
        "step": "UPDATE todo_steps SET status=%s, updated_at=now() WHERE id=%s",
        "task": "UPDATE todo_tasks SET status=%s, updated_at=now() WHERE id=%s"
    }
    query = query_map.get(entity_type.lower())
    if not query:
        raise ValueError("entity_type must be one of project, workflow, step, task")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (status, entity_id))
            if cur.rowcount == 0:
                raise ValueError(f"{entity_type} {entity_id} not found")
    return {"entity_type": entity_type, "entity_id": entity_id, "status": status}

@mcp.tool(name="todo.assign_to_subagent")
def todo_assign_to_subagent(task_id: str, worker: str, mode: Optional[str] = None, bank_id: Optional[str] = None, mcp_allowlist: Optional[list] = None, model_hint: Optional[str] = None) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Build metadata payload for extra assignment info
            meta = {}
            if mode:
                meta["mode"] = mode
            if bank_id:
                meta["bank_id"] = bank_id
            if mcp_allowlist:
                meta["mcp_allowlist"] = mcp_allowlist
            # Merge with existing metadata
            cur.execute(
                """UPDATE todo_tasks
                   SET assigned_worker=%s,
                       assigned_subagent=%s,
                       assigned_at=now(),
                       status='in_progress',
                       model_hint=COALESCE(%s, model_hint),
                       metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                       updated_at=now()
                   WHERE id=%s
                   RETURNING id, assigned_worker""",
                (worker, worker, model_hint, json.dumps(meta), task_id)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("task not found")
    return {"task_id": task_id, "assigned_worker": worker, "mode": mode, "bank_id": bank_id}

@mcp.tool(name="todo.get_agent_load")
def todo_get_agent_load(worker: Optional[str] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if worker:
                cur.execute("SELECT * FROM todo_agent_load WHERE assigned_worker=%s", (worker,))
            else:
                cur.execute("SELECT * FROM todo_agent_load")
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.list_projects")
def todo_list_projects(bank_id: Optional[str] = None, status: Optional[str] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = "SELECT id, name, status, bank_id FROM todo_projects WHERE 1=1"
            params = []
            if bank_id:
                query += " AND bank_id = %s"
                params.append(bank_id)
            if status:
                query += " AND status = %s"
                params.append(status)
            query += " ORDER BY created_at DESC"
            cur.execute(query, params)
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.list_workflows")
def todo_list_workflows(project_id: Optional[str] = None, bank_id: Optional[str] = None, status: Optional[str] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = "SELECT id, project_id, name, status, bank_id FROM todo_workflows WHERE 1=1"
            params = []
            if project_id:
                query += " AND project_id = %s"
                params.append(project_id)
            if bank_id:
                query += " AND bank_id = %s"
                params.append(bank_id)
            if status:
                query += " AND status = %s"
                params.append(status)
            cur.execute(query, params)
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.list_steps")
def todo_list_steps(workflow_id: Optional[str] = None, status: Optional[str] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = "SELECT id, workflow_id, name, order_idx, status, depends_on, metadata FROM todo_steps WHERE 1=1"
            params = []
            if workflow_id:
                query += " AND workflow_id = %s"
                params.append(workflow_id)
            if status:
                query += " AND status = %s"
                params.append(status)
            query += " ORDER BY order_idx"
            cur.execute(query, params)
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.list_tasks")
def todo_list_tasks(step_id: Optional[str] = None, assigned_worker: Optional[str] = None, status: Optional[str] = None) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = "SELECT id, step_id, name, status, assigned_worker, model_hint, resource, metadata FROM todo_tasks WHERE 1=1"
            params = []
            if step_id:
                query += " AND step_id = %s"
                params.append(step_id)
            if assigned_worker:
                query += " AND assigned_worker = %s"
                params.append(assigned_worker)
            if status:
                query += " AND status = %s"
                params.append(status)
            cur.execute(query, params)
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.get_step_dependencies")
def todo_get_step_dependencies(step_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, depends_on FROM todo_steps WHERE id=%s", (step_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"step {step_id} not found")
            return {"id": row["id"], "name": row["name"], "depends_on": row["depends_on"] or []}

@mcp.tool(name="todo.update_metadata")
def todo_update_metadata(entity_type: str, entity_id: str, metadata: dict) -> dict:
    query_map = {
        "project": "UPDATE todo_projects SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s",
        "workflow": "UPDATE todo_workflows SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s",
        "step": "UPDATE todo_steps SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s",
        "task": "UPDATE todo_tasks SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s"
    }
    query = query_map.get(entity_type.lower())
    if not query:
        raise ValueError("entity_type must be one of project, workflow, step, task")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (json.dumps(metadata), entity_id))
            if cur.rowcount == 0:
                raise ValueError(f"{entity_type} {entity_id} not found")
    return {"entity_type": entity_type, "entity_id": entity_id, "metadata_updated": True}

@mcp.tool(name="todo.update_depends_on")
def todo_update_depends_on(step_id: str, depends_on: list) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todo_steps SET depends_on = %s::uuid[], updated_at=now() WHERE id=%s", (depends_on, step_id))
            if cur.rowcount == 0:
                raise ValueError(f"step {step_id} not found")
    return {"step_id": step_id, "depends_on": depends_on}

@mcp.tool(name="todo.bulk_update_status")
def todo_bulk_update_status(entity_type: str, entity_ids: list, status: str) -> dict:
    query_map = {
        "project": "UPDATE todo_projects SET status=%s, updated_at=now() WHERE id = ANY(%s)",
        "workflow": "UPDATE todo_workflows SET status=%s, updated_at=now() WHERE id = ANY(%s)",
        "step": "UPDATE todo_steps SET status=%s, updated_at=now() WHERE id = ANY(%s)",
        "task": "UPDATE todo_tasks SET status=%s, updated_at=now() WHERE id = ANY(%s)"
    }
    query = query_map.get(entity_type.lower())
    if not query:
        raise ValueError("entity_type must be one of project, workflow, step, task")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (status, entity_ids))
    return {"entity_type": entity_type, "updated_count": cur.rowcount, "status": status}

@mcp.tool(name="todo.get_inflight_agents")
def todo_get_inflight_agents() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT assigned_worker, COUNT(*) as in_progress
                FROM todo_tasks
                WHERE status='in_progress' AND assigned_worker IS NOT NULL
                GROUP BY assigned_worker
            """)
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.search_tasks")
def todo_search_tasks(query: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            like = f"%{query}%"
            cur.execute("""
                SELECT id, step_id, name, status, assigned_worker
                FROM todo_tasks
                WHERE name ILIKE %s OR metadata::text ILIKE %s
            """, (like, like))
            rows = cur.fetchall()
    return rows

@mcp.tool(name="todo.get_workflow_hierarchy")
def todo_get_workflow_hierarchy(project_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status FROM todo_workflows WHERE project_id=%s", (project_id,))
            workflows = cur.fetchall()
            result = {"project_id": project_id, "workflows": []}
            for wf in workflows:
                cur.execute("SELECT id, name, order_idx, status FROM todo_steps WHERE workflow_id=%s ORDER BY order_idx", (wf["id"],))
                steps = cur.fetchall()
                wf_data = {"id": wf["id"], "name": wf["name"], "status": wf["status"], "steps": []}
                for s in steps:
                    cur.execute("SELECT id, name, status, assigned_worker FROM todo_tasks WHERE step_id=%s", (s["id"],))
                    tasks = cur.fetchall()
                    wf_data["steps"].append({"id": s["id"], "name": s["name"], "order_idx": s["order_idx"], "status": s["status"], "tasks": tasks})
                result["workflows"].append(wf_data)
    return result

@mcp.tool(name="todo.delete_task")
def todo_delete_task(task_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.id, t.status, w.bank_id
                   FROM todo_tasks t
                   JOIN todo_steps s ON t.step_id = s.id
                   JOIN todo_workflows w ON s.workflow_id = w.id
                   WHERE t.id=%s""",
                (task_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"task {task_id} not found")
            if BANK_ID and row["bank_id"] and row["bank_id"] != BANK_ID:
                raise PermissionError("unauthorized: bank mismatch")
            if row["status"] in {"locked", "protected"}:
                raise PermissionError("task is protected")
            cur.execute("DELETE FROM todo_tasks WHERE id=%s", (task_id,))
    return {"id": task_id, "deleted": True}

@mcp.tool(name="todo.delete_step")
def todo_delete_step(step_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.status, w.bank_id
                   FROM todo_steps s
                   JOIN todo_workflows w ON s.workflow_id = w.id
                   WHERE s.id=%s""",
                (step_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"step {step_id} not found")
            if BANK_ID and row["bank_id"] and row["bank_id"] != BANK_ID:
                raise PermissionError("unauthorized: bank mismatch")
            if row["status"] in {"locked", "protected"}:
                raise PermissionError("step is protected")
            cur.execute("DELETE FROM todo_tasks WHERE step_id=%s", (step_id,))
            deleted_tasks = cur.rowcount
            cur.execute("DELETE FROM todo_steps WHERE id=%s", (step_id,))
    return {"id": step_id, "deleted_tasks": deleted_tasks}

@mcp.tool(name="todo.delete_workflow")
def todo_delete_workflow(workflow_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, bank_id, status FROM todo_workflows WHERE id=%s",
                (workflow_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"workflow {workflow_id} not found")
            if BANK_ID and row["bank_id"] and row["bank_id"] != BANK_ID:
                raise PermissionError("unauthorized: bank mismatch")
            if row["status"] in {"locked", "protected"}:
                raise PermissionError("workflow is protected")
            cur.execute("SELECT id FROM todo_steps WHERE workflow_id=%s", (workflow_id,))
            steps = cur.fetchall()
            step_ids = [s["id"] for s in steps]
            deleted_tasks = 0
            if step_ids:
                cur.execute("DELETE FROM todo_tasks WHERE step_id = ANY(%s)", (step_ids,))
                deleted_tasks = cur.rowcount
            deleted_steps = 0
            if step_ids:
                cur.execute("DELETE FROM todo_steps WHERE workflow_id=%s", (workflow_id,))
                deleted_steps = cur.rowcount
            cur.execute("DELETE FROM todo_workflows WHERE id=%s", (workflow_id,))
    return {"id": workflow_id, "deleted_steps": deleted_steps, "deleted_tasks": deleted_tasks}

@mcp.tool(name="todo.delete_project")
def todo_delete_project(project_id: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, bank_id, status FROM todo_projects WHERE id=%s",
                (project_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"project {project_id} not found")
            if BANK_ID and row["bank_id"] and row["bank_id"] != BANK_ID:
                raise PermissionError("unauthorized: bank mismatch")
            if row["status"] in {"locked", "protected"}:
                raise PermissionError("project is protected")
            cur.execute("SELECT id FROM todo_workflows WHERE project_id=%s", (project_id,))
            workflows = cur.fetchall()
            workflow_ids = [w["id"] for w in workflows]
            step_ids = []
            if workflow_ids:
                cur.execute("SELECT id FROM todo_steps WHERE workflow_id = ANY(%s)", (workflow_ids,))
                steps = cur.fetchall()
                step_ids = [s["id"] for s in steps]
            deleted_tasks = 0
            if step_ids:
                cur.execute("DELETE FROM todo_tasks WHERE step_id = ANY(%s)", (step_ids,))
                deleted_tasks = cur.rowcount
            deleted_steps = 0
            if workflow_ids:
                cur.execute("DELETE FROM todo_steps WHERE workflow_id = ANY(%s)", (workflow_ids,))
                deleted_steps = cur.rowcount
            deleted_workflows = 0
            if workflow_ids:
                cur.execute("DELETE FROM todo_workflows WHERE project_id=%s", (project_id,))
                deleted_workflows = cur.rowcount
            cur.execute("DELETE FROM todo_projects WHERE id=%s", (project_id,))
    return {"id": project_id, "deleted_workflows": deleted_workflows, "deleted_steps": deleted_steps, "deleted_tasks": deleted_tasks}

# ---------- Resources ----------
@mcp.resource("todos://projects")
def projects_list() -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if BANK_ID:
                cur.execute("SELECT id, name, status, bank_id FROM todo_projects WHERE bank_id = %s ORDER BY created_at DESC", (BANK_ID,))
            else:
                cur.execute("SELECT id, name, status, bank_id FROM todo_projects ORDER BY created_at DESC")
            projects = cur.fetchall()
    md = "# Projects\n\n"
    for p in projects:
        md += f"- [{p['status']}] {p['name']} (`{p['id']}`) bank:{p['bank_id'] or 'none'}\n"
    return md or "# Projects\n\n_No projects found_"

@mcp.resource("todos://projects/{project_id}")
def project_markdown(project_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if BANK_ID:
                cur.execute("SELECT id, name, description, status, created_at, updated_at FROM todo_projects WHERE id=%s AND bank_id = %s", (project_id, BANK_ID))
            else:
                cur.execute("SELECT id, name, description, status, created_at, updated_at FROM todo_projects WHERE id=%s", (project_id,))
            proj = cur.fetchone()
            if not proj:
                return f"# Project {project_id} not found"
            if BANK_ID:
                cur.execute("SELECT id, name, status FROM todo_workflows WHERE project_id=%s AND bank_id = %s", (project_id, BANK_ID))
            else:
                cur.execute("SELECT id, name, status FROM todo_workflows WHERE project_id=%s", (project_id,))
            wfs = cur.fetchall()
    md = f"# Project: {proj['name']}\n\n"
    md += f"**ID:** {proj['id']}\n**Status:** {proj['status']}\n\n"
    md += f"{proj['description'] or ''}\n\n"
    md += "## Workflows\n"
    for wf in wfs:
        md += f"- [{wf['status']}] {wf['name']} (`{wf['id']}`)\n"
    return md

@mcp.resource("todos://projects/{project_id}/workflows")
def project_workflows(project_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if BANK_ID:
                cur.execute("SELECT id, name, status, bank_id FROM todo_workflows WHERE project_id=%s AND bank_id = %s", (project_id, BANK_ID))
            else:
                cur.execute("SELECT id, name, status, bank_id FROM todo_workflows WHERE project_id=%s", (project_id,))
            wfs = cur.fetchall()
    md = f"# Workflows for Project {project_id}\n\n"
    for wf in wfs:
        md += f"- [{wf['status']}] {wf['name']} (`{wf['id']}`) bank:{wf['bank_id'] or 'none'}\n"
    return md or f"# Workflows for Project {project_id}\n\n_No workflows found_"

@mcp.resource("todos://workflows/{workflow_id}")
def workflow_markdown(workflow_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if BANK_ID:
                cur.execute("SELECT id, name, status, project_id FROM todo_workflows WHERE id=%s AND bank_id = %s", (workflow_id, BANK_ID))
            else:
                cur.execute("SELECT id, name, status, project_id FROM todo_workflows WHERE id=%s", (workflow_id,))
            wf = cur.fetchone()
            if not wf:
                return f"# Workflow {workflow_id} not found"
            cur.execute("SELECT id, name, order_idx, status FROM todo_steps WHERE workflow_id=%s ORDER BY order_idx", (workflow_id,))
            steps = cur.fetchall()
    md = f"# Workflow: {wf['name']}\n\n**ID:** {wf['id']}\n**Status:** {wf['status']}\n\n## Steps\n"
    for s in steps:
        md += f"- [{s['status']}] {s['name']} (`{s['id']}`)\n"
    return md

@mcp.resource("todos://workflows/{workflow_id}/steps")
def workflow_steps(workflow_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, order_idx, status FROM todo_steps WHERE workflow_id=%s ORDER BY order_idx", (workflow_id,))
            steps = cur.fetchall()
    md = f"# Steps for Workflow {workflow_id}\n\n"
    for s in steps:
        md += f"- [{s['status']}] {s['name']} (order {s['order_idx']}) (`{s['id']}`)\n"
    return md or f"# Steps for Workflow {workflow_id}\n\n_No steps found_"

@mcp.resource("todos://steps/{step_id}")
def step_markdown(step_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status, workflow_id FROM todo_steps WHERE id=%s", (step_id,))
            step = cur.fetchone()
            if not step:
                return f"# Step {step_id} not found"
            cur.execute("SELECT id, name, status, assigned_worker FROM todo_tasks WHERE step_id=%s", (step_id,))
            tasks = cur.fetchall()
    md = f"# Step: {step['name']}\n\n**ID:** {step['id']}\n**Status:** {step['status']}\n\n## Tasks\n"
    for t in tasks:
        assign = f" → {t['assigned_worker']}" if t['assigned_worker'] else ""
        md += f"- [{t['status']}] {t['name']}{assign} (`{t['id']}`)\n"
    return md

@mcp.resource("todos://steps/{step_id}/tasks")
def step_tasks(step_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status, assigned_worker, model_hint, resource FROM todo_tasks WHERE step_id=%s", (step_id,))
            tasks = cur.fetchall()
    md = f"# Tasks for Step {step_id}\n\n"
    for t in tasks:
        assign = f" → {t['assigned_worker']}" if t['assigned_worker'] else ""
        md += f"- [{t['status']}] {t['name']}{assign} (`{t['id']}`) model:{t['model_hint'] or 'none'} resource:{t['resource'] or 'none'}\n"
    return md or f"# Tasks for Step {step_id}\n\n_No tasks found_"

@mcp.resource("todos://tasks/{task_id}")
def task_markdown(task_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_tasks WHERE id=%s", (task_id,))
            task = cur.fetchone()
            if not task:
                return f"# Task {task_id} not found"
    md = f"# Task: {task['name']}\n\n"
    md += f"**ID:** {task['id']}\n**Status:** {task['status']}\n"
    md += f"**Assigned:** {task['assigned_worker'] or 'unassigned'}\n"
    md += f"**Subagent:** {task['assigned_subagent'] or 'none'}\n"
    return md

@mcp.resource("todos://agent-load")
def agent_load_resource() -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT assigned_worker, in_progress_tasks, pending_tasks, done_tasks, updated_at FROM todo_agent_load ORDER BY assigned_worker")
            rows = cur.fetchall()
    md = "# Agent Load\n\n"
    for r in rows:
        md += f"- {r['assigned_worker']}: in_progress={r['in_progress_tasks']}, pending={r['pending_tasks']}, done={r['done_tasks']} (updated {r['updated_at']})\n"
    return md or "# Agent Load\n\n_No agent load data_"

if __name__ == "__main__":
    mcp.run()
