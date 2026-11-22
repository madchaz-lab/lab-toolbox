# kurisu-harness

The operating harness for **Makise Kurisu**, the Madchaz lab's orchestrator
agent (opencode v2, running on her own VM, directing specialist subagents
against a Kubernetes homelab). Published 2026-11-22, at the state of
**batch 10** (hindsight multi-bank MCP), so the setup can be replicated or
rebuilt from scratch: code, schemas, manifests, systemd units, behavior
rules, and skills — with every credential replaced by a placeholder or an
environment reference.

## Components

| Path | What it is |
|------|------------|
| `todo-tool/` | The board: a 4-level todo tool (project / task / step / work order) as an MCP server over Postgres. HMAC class tokens (orchestrator / step-owner / worker / validator / executor), mutation revisions with CAS, template registry with audit-required metadata, the **slot grant** (one running worker per GPU node, granted by the tool), a VM-local **executor** that grades declared probes against the deployed system, and deny-only guards (probe, audit, destructive). `server.py` is the whole server; `tests/battery_v4.py` is its acceptance battery (77 checks); `class_crossing_recipe.py` is the raw-HTTP fallback client. |
| `hindsight-filtering-proxy/` | A path-preserving MCP proxy in front of Hindsight. It removes the destructive trio (`clear_memories`, `delete_bank`, `clear_mental_model`) from the catalog and refuses calls to them on every bank path, and enforces a read-only path for `chronoa-singlebank`. The agent's opencode config carries one MCP entry per bank, all routed through this proxy. |
| `behaviors/` | The behavior layer: `AGENTS.md` (the canonical ruleset — dispatch cap, slot-grant choreography, closure discipline, bank doctrine), the generated agent prompts (`agents/`: orchestrator, seven specialists, validator), the loadable skills (`skills/`: todo-tool mechanics, hindsight mechanics, post-compaction refresh), and `opencode.example.json` (the full config shape, secrets as placeholders / `{env:…}` references). |
| `heartbeat/` | A systemd timer + script that wakes the orchestrator when the board holds actionable work (open orders, ready steps, unread mail) and she is idle. A backstop, never the loop's engine. |
| `chronoa/` | Support for Chronoa's weekly paper: her **curated proxy** (single-bank, trio refused, read-write otherwise — a separate codebase from the filtering proxy), the 6-hour run watchdog (mail alert, never a kill), and the `chronoa-weekly` CronJob manifest. |

## How it fits together

The orchestrator works a single board queue. Every dispatch takes a slot
grant from the todo tool (one running worker per node); workers act under
work-order-bound credentials; closures are per-element and validator-gated;
declared probes are graded by the executor against the live system, and
validation refuses until the verdict and the post-acceptance confirmation
agree. Memory lives in Hindsight banks (one orchestrator bank, one per
specialist desk), reached only through the filtering proxy, so destructive
memory operations do not exist on the agent's surface. The heartbeat wakes
her if the loop stalls; worker returns and the board drive the loop.

See `INSTALL.md` for the replication order and the secrets inventory
(names only — values are never committed, per the repo convention).

## AI Attribution

This harness was designed with madchaz and built by Muse (Muse Spark,
Meta) acting as his directing/building agent during the 2026-11-21/22
build, with the lab's local models (Glimmer 30B) running the orchestrator
it describes. Human direction and acceptance: madchaz.
