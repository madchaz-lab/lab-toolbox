# washu-todo MCP Server

FastMCP server providing CRUD, assignment and agent-load for todo_projects, todo_workflows, todo_steps, todo_tasks.

## Files
- `sql/schema_extension.sql` – DB extensions
- `src/todo_mcp_server.py` – FastMCP server
- `deploy/deployment.yaml` – Kubernetes manifest for brains-containment
- `docs/implementation_plan.md` – Design plan

## Run locally
```bash
export WASHU_POSTGRES_DSN=postgresql://...
python src/todo_mcp_server.py
```

## MCP Tools
todo.create_project, todo.create_workflow, todo.create_step, todo.create_task, todo.update_status, todo.assign_to_subagent, todo.get_agent_load, todo.delete_task, todo.delete_step, todo.delete_workflow, todo.delete_project

## MCP Resources
todos://projects/{id}, todos://workflows/{id}, todos://steps/{id}, todos://tasks/{id}
