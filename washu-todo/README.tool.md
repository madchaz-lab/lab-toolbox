# washu-todo MCP Tool

Todo management MCP server backed by washu-postgres with OpenCode right-pane integration.

## Files included
- `src/todo_mcp_server.py` – MCP server implementation
- `sql/schema_extension.sql` – Database schema extensions
- `requirements.txt` – Python dependencies
- `.env.example` – Environment variables
- `install.md` – Installation instructions

## Install
See `install.md`.

## Database setup
Apply schema via washu-postgres MCP:
```
# Use washu-postgres MCP execute tool to apply schema_extension.sql
# Do not use direct psql
```

## Usage
Run MCP server:
```
python src/todo_mcp_server.py
```

## Skill
This tool is used with OpenCode via the `washu-todo` MCP server and optional right-pane plugin `opencode-todo-rightpane`.
