# Dockerfile — basic-memory MCP server + basic-memory-viewer in one container.
#
# Shared store: enkel /data (named volume, see docker-compose.yml).
#   - basic-memory      : MCP server, streamable-http on 8765, path /mcp
#   - basic-memory-viewer : FastAPI UI on 8200 talking to the MCP over localhost
#
# Versions:
#   - basic-memory 0.23.2 (PyPI; official install method is "uv tool install
#     --prerelease=allow"; the flag is required because 0.23 depends on a
#     FastMCP 4 pre-release)
#   - basic-memory-viewer main @ 93d1be822ad1 (no PyPI package — official method
#     is pip install -r requirements.txt from the repo)

FROM python:3.12-slim

# git: viewer needs it (per-note file history / snapshots inside NOTES_DIR).
# curl: entrypoint health-wait on the MCP endpoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# uv is the officially recommended installer for basic-memory.
RUN pip install --no-cache-dir uv

# --- Process 1: basic-memory MCP server (python 3.12+); uv installs the CLI
# into $HOME/.local/bin (HOME=/root during build).
RUN uv tool install --prerelease=allow basic-memory==0.23.2

# --- Process 2: basic-memory-viewer UI (source install, official method)
RUN git clone --depth 1 https://github.com/manelpb/basic-memory-viewer /opt/basic-memory-viewer \
    && git -C /opt/basic-memory-viewer checkout 93d1be822ad1 \
    && pip install --no-cache-dir -r /opt/basic-memory-viewer/requirements.txt

# Entrypoint starts both processes (MCP in background, viewer in foreground).
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Keep uv tool binaries on PATH regardless of runtime HOME.
ENV PATH="/root/.local/bin:${PATH}"

# Runtime data goes under /data (named volume). HOME=/data makes basic-memory
# seed its default "main" project at /data/basic-memory on first start, which
# matches NOTES_DIR. BASIC_MEMORY_CONFIG_DIR (official env) keeps its config and
# SQLite index inside the volume too. Auto-update/telemetry disabled for a
# stable container.
ENV HOME=/data \
    BASIC_MEMORY_CONFIG_DIR=/data/.basic-memory \
    BASIC_MEMORY_AUTO_UPDATE=false \
    BASIC_MEMORY_NO_PROMOS=1 \
    NOTES_DIR=/data/basic-memory \
    MCP_URL=http://localhost:8765/mcp \
    MCP_TRANSPORT=http \
    BM_PROJECT=main
# APP_TITLE spelled in legacy "ENV key value" form so the classic builder's
# name=value parser (used where buildx/BuildKit is unavailable) accepts the
# value's space instead of failing on the bare token.
ENV APP_TITLE Basic Memory

EXPOSE 8765 8200

ENTRYPOINT ["entrypoint.sh"]