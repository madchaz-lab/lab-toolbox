# Install washu-todo

## Prerequisites

- Python 3.11+
- PostgreSQL access via washu-postgres MCP
- Kubernetes cluster for deployment (optional)

## Local setup

1. Clone repository
2. Copy environment file
   ```bash
   cp .env.example .env
   ```
   Fill values from vault; do not commit secrets.
3. Install dependencies
   ```bash
   pip install -r requirements.txt
   ```
4. Apply schema via washu-postgres MCP
   ```bash
   # Use washu-postgres MCP execute tool to apply schema_extension.sql
   # Example: run migration via MCP, not direct psql
   # mcp washu-postgres execute --file sql/schema_extension.sql
   ```
   Do not use direct psql or expose DSN. Schema changes must be executed through washu-postgres MCP within cluster trust boundary.
5. Run MCP server
   ```bash
   python src/todo_mcp_server.py
   ```

## Deployment

Apply Kubernetes manifest:
```bash
kubectl apply -f deploy/deployment.yaml
```

Secrets are referenced by name only; values are injected via Kubernetes Secrets / Vault CSI inside cluster trust boundary.

## Verify

Run tests:
```bash
pytest tests/ --cov=src
```

See `docs/design/test_plan.md` for test coverage expectations.
