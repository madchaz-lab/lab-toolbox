#!/usr/bin/env bash
# entrypoint.sh — starts both memory processes inside the single container.
#
#  1. basic-memory MCP server      → background, streamable-http on :8765
#  2. basic-memory-viewer (uvicorn)→ foreground (PID 1), web UI on :8200
#
# Supervisor-less: the MCP runs in the background and the UI blocks as PID 1,
# so container lifetime == viewer lifetime; docker stop kills the whole tree.

set -euo pipefail

# --- Process 1: basic-memory MCP server (streamable-http on :8765, path /mcp)
echo "[memory-mcp] starting basic-memory MCP server (streamable-http :8765)"
basic-memory mcp \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8765 &

# --- Wait for the MCP endpoint to accept connections before starting the UI.
# The viewer's readiness probe (/healthz) checks the MCP, but a short wait gives
# the server a head start on first boot (db init + index build).
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://localhost:8765/mcp" 2>/dev/null; then
    break
  fi
  sleep 1
done
echo "[memory-mcp] starting basic-memory-viewer (uvicorn :8200)"

# --- Process 2: viewer in the foreground; exec so it becomes PID 1.
exec uvicorn app.main:app \
  --app-dir /opt/basic-memory-viewer \
  --host 0.0.0.0 \
  --port 8200