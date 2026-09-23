# memory-mcp — basic-memory MCP server + basic-memory-viewer (single container)

> Status: **WORK IN PROGRESS** — deployed and functionally verified on
> dragonball04, but the opencode MCP hookup is not live until opencode is
> restarted. See [Status / verification](#status--verification).

## Index

- [Two-process architecture](#two-process-architecture) — how the MCP server and viewer share one store
- [Ports](#ports) — container and host port mapping
- [Persistent storage](#persistent-storage) — the `basic-memory-data` volume
- [Build and run](#build-and-run) — both build paths (Path A: buildx, Path B: legacy parser)
- [Reach the viewer UI](#reach-the-viewer-ui) — web UI (8200)
- [Reach the MCP endpoint from opencode](#reach-the-mcp-endpoint-from-opencode) — streamable-http (8765) wiring
- [Environment reference](#environment-reference) — env vars, defaults, and what uses them
- [Files](#files) — the shipped files
- [Status / verification](#status--verification) — what is verified, what is pending

One Docker container on **dragonball04** that runs both memory services against
one persistent store:

- **basic-memory** (`basicmachines-co/basic-memory`, v0.23.2) — MCP server.
  Notes are plain Markdown; a local SQLite index powers knowledge-graph and
  semantic search. Runs on the **streamable-http** transport so remote MCP
  clients (opencode) can reach it over the network.
- **basic-memory-viewer** (`manelpb/basic-memory-viewer`, main @
  `93d1be822ad1`) — read-only FastAPI web UI. It opens a short-lived MCP
  session per request against the same basic-memory instance, so the browser
  view always reflects the live store.

## Two-process architecture

```
                        ┌────────────────── container ──────────────────┐
 opencode / MCP client  │  basic-memory mcp        basic-memory-viewer   │
 ── streamable-http ──▶ │  :8765/mcp  ── localhost ──▶ uvicorn app.main   │
   (host 192.168.27.24) │                 :8765/mcp     :8200            │
   (dragonball04:8765)  └──────────────────┬─────────────────────────────┘
                                           │  share /data (named volume)
                                     Markdown notes + SQLite + git history
```

Both processes read and write the same directory tree (`/data/basic-memory`),
so the UI and the MCP tools are always in sync. `entrypoint.sh` (supervisor-less)
starts the MCP server in the background, waits for its endpoint, then execs the
viewer as PID 1.

## Ports

| Service | Container | Host (dragonball04) | Changed from default? |
|---|---|---|---|
| basic-memory MCP (streamable-http, path `/mcp`) | `8765` | `8765` | yes — default MCP http port is `8000` |
| basic-memory-viewer (uvicorn) | `8200` | `8200` | yes — upstream example is `8000` |

Both host ports were checked **free** on dragonball04 on 2026-09-23
(`ss -tln` + `docker ps`; only `4000`/litellm, `22`, `53`, `8080`/llama are in
use). `HOST_MCP_PORT` / `HOST_VIEWER_PORT` can be overridden in `.env`.

> The viewer has **no authentication** (upstream warning). Keep it on the lab
> LAN; do not expose it to the internet.

## Persistent storage

Named Docker volume **`basic-memory-data`**, mounted at `/data`:

- `/data/basic-memory` — Markdown notes (basic-memory "main" project)
- `/data/.basic-memory` — basic-memory config + SQLite index
- viewer git history snapshots live inside `/data/basic-memory/.git`

The volume survives container rebuilds and restarts. Back it up by copying the
volume or rsyncing `/data/basic-memory`.

## Build and run

```bash
cp .env.example .env   # optional — defaults already match
```

Two build paths exist; **both were used on dragonball04 on 2026-09-23**:

```bash
# Path A — compose-managed build + run (use this where buildx is available)
docker compose up -d --build

# Path B — legacy-parser build, then compose run (db04)
# db04's docker daemon has NO buildx / BuildKit, so the classic builder parses
# the Dockerfile. Build the image first, then bring the stack up:
docker build -t memory-mcp .
docker compose up -d
```

Why Path B exists: the legacy builder's `ENV KEY=VALUE` parser rejects a value
containing a space (e.g. `ENV APP_TITLE="Basic Memory"` fails). The Dockerfile
therefore spells the title in the legacy form — `ENV APP_TITLE Basic Memory` —
which the classic parser accepts.

```bash
docker compose ps
docker compose logs -f
```

Stopping: `docker compose down` (leaves the volume intact). To rebuild after
dependency updates: `docker compose up -d --build`.

## Reach the viewer UI

http://192.168.27.24:8200 — browse notes, search, and per-note history.

## Reach the MCP endpoint from opencode

The MCP endpoint is a streamable-http server at:

```
http://192.168.27.24:8765/mcp
```

It is wired into opencode (bulma, host "androids") in `~/.config/opencode/
opencode.json` under `mcp`:

```json
"basic-memory": {
  "type": "http",
  "url": "http://192.168.27.24:8765/mcp"
}
```

opencode loads MCP servers only at session startup, so this entry takes effect
at the next opencode restart (pending — see Status below). It carries full
read/write tools (e.g. `write_note`, `search_notes`) — keep it LAN-only.

## Environment reference

| Variable | Default | Used by |
|---|---|---|
| `BASIC_MEMORY_CONFIG_DIR` | `/data/.basic-memory` | basic-memory (config + app DB live in the volume) |
| `BASIC_MEMORY_AUTO_UPDATE` | `false` | disable background self-updates in the container |
| `BASIC_MEMORY_NO_PROMOS` | `1` | disable promos/telemetry |
| `NOTES_DIR` | `/data/basic-memory` | viewer (file history + descriptions from disk) |
| `MCP_URL` | `http://localhost:8765/mcp` | viewer → MCP inside the container |
| `MCP_TRANSPORT` | `http` | viewer → streamable-http |
| `BM_PROJECT` | `main` | viewer default project |
| `APP_TITLE` | `Basic Memory` | viewer sidebar label |
| `HOME` | `/data` | makes basic-memory seed its project at `/data/basic-memory` |

Note: the `BASIC_MEMORY_HOME` env var from third-party write-ups is **not** in
the official basic-memory docs; this image instead uses the documented
`BASIC_MEMORY_CONFIG_DIR` + a `main` project path driven by `HOME` so that all
data lands under `/data`.

## Files

```
docker-compose.yml   service + named volume + port mapping
Dockerfile           installs both tools (pinned versions)
entrypoint.sh        starts MCP (bg) then viewer (PID 1)
.env.example         tunables (no secrets)
```

## Status / verification

**Status: WORK IN PROGRESS** — not yet marked "tested and confirmed working"
end to end because the last piece — the opencode `basic-memory` MCP entry —
only activates at the next opencode restart and has not been exercised
in-session yet.

What **is** verified (dragonball04, 2026-09-23):

- Image builds on db04 with the **legacy (non-buildx)** parser and runs as
  container `memory-mcp` with `restart: unless-stopped`.
- Endpoints respond: `POST http://192.168.27.24:8765/mcp` initialize → 200
  (serverInfo "Basic Memory 4.0.0b1"; 21 tools incl. `write_note`,
  `read_note`, `search_notes`); viewer `http://192.168.27.24:8200` → 200.
- Write → read round-trip OK (note persisted); search returned it.
- Persistence across `docker restart memory-mcp`: note + markers survived;
  named volume `basic-memory-data` confirmed via `docker volume inspect`.
- Compose file validates: `docker compose config` → OK (local VM + db04).

Evidence: the build, endpoint, round-trip, and persistence checks below were
run on dragonball04 on 2026-09-23; scratch notes (compose config, deploy,
verify, opencode wiring, raw MCP exchange) live in the lab's private
`tmp/` and are not shipped with this repo.

Pending: restart opencode on androids, then confirm `basic-memory` appears as
an MCP tool server in-session.

## AI Attribution

This work was generated by a human-guided AI assistant. The model is
Qwen3.8-27B-Q4 (pooled/qwen3.8-27b-q4), hosted locally on two NVIDIA
GeForce GTX 1080 Ti GPUs (drivers 580.159.03 and 580.173.02).
No cloud APIs were used.