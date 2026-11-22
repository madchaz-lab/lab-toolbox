# Installing the Kurisu harness

A replication outline, in dependency order. It assumes a k3s (or any
Kubernetes) cluster, a Hindsight deployment, and a VM for the agent with
opencode v2.3.2. Namespaces, node names, model endpoints, and addresses in
the shipped files are the lab's own — adjust them to your site.

## 1. Secrets first (names only; create your own values)

| Secret | Namespace | Keys | Used by |
|--------|-----------|------|---------|
| `kurisu-postgres-creds` | `brains-containment` | `username`, `password` | Postgres + todo server |
| `kurisu-todo-creds` | `brains-containment` | `token-secret` | todo server (HMAC signing key for class tokens) |
| `hindsight-secrets` | `brains-containment` | `tenant-api-key` | filtering proxy clients (the agent's `{env:HINDSIGHT_TENANT_KEY}`) |
| `kurisu-mail-creds` | `brains-containment` | account password | Chronoa watchdog's mail alert |

The orchestrator's own todo token is minted from `token-secret`'s signing
key; in `opencode.example.json` it is the `CHANGE_ME` value.

## 2. The board (todo tool)

1. Apply `todo-tool/manifests/kurisu-postgres.yaml` (its own PVC), then
   load `todo-tool/schema.sql` + `schema-v2.sql` (+ the ontology
   migration) into the database.
2. Build the code ConfigMap from `todo-tool/server.py` and apply
   `kurisu-todo-deployment.yaml` + `kurisu-todo-service.yaml`. The pod
   pip-installs its pinned dependencies at start; allow ~75 s before the
   MCP endpoint answers.
3. Verify with the acceptance battery:
   `python3 todo-tool/tests/battery_v4.py <todo-mcp-base-url> <token-secret>`
   — 77/77 at publication.

## 3. VM-local services (systemd user units)

- `todo-tool/todo_executor.py` + `todo-tool/systemd/todo-executor.service`:
  polls the board for `done_proposed` work orders carrying a probe spec,
  runs the probe as data (closed allowlists), records verdict +
  post-acceptance confirmation.
- `todo-tool/slot_reconciler.py` + `slot-reconciler.{service,timer}`:
  releases slot grants whose sessions finished or went quiet; never kills
  or pauses anything.
- `hindsight-filtering-proxy/filtering_proxy.py` +
  `hindsight-filtering-proxy/systemd/kurisu-filtering-proxy.service`:
  listens on 127.0.0.1:8892, forwards `/mcp/<bank>/` paths to the
  Hindsight MCP endpoint, refuses the destructive trio everywhere and all
  write verbs on the `chronoa-singlebank` path.

## 4. The agent

1. Place `behaviors/AGENTS.md` in the agent's home, the prompts from
   `behaviors/agents/` and skills from `behaviors/skills/` where the
   config expects them, and adapt `behaviors/opencode.example.json`
   (model providers/endpoints, the todo token, the tenant-key env var).
2. Launch opencode in a tmux session. The config's per-bank hindsight
   entries appear in the MCP panel once connected.

## 5. The backstop

Install `heartbeat/kurisu-heartbeat.sh` to `/usr/local/bin/` and enable
the **system** units in `heartbeat/systemd/` (10-minute ticks). The
heartbeat is a backstop: it wakes an idle orchestrator when the board
holds work; it carries no authority and changes nothing about what the
agent may do.

## 6. Chronoa (optional)

`chronoa/chronoa_curated_proxy.py` runs beside the weekly runner
(127.0.0.1:8891, single bank, read-write, trio refused);
`chronoa/chronoa-weekly-cronjob.yaml` is the Sunday 01:00 America/Toronto
run; `chronoa/watchdog_6h.py` mails an alert if a run exceeds six hours.
There is deliberately **no run timeout** anywhere in this harness.
