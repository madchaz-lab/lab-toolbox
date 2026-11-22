# Filtering Proxy Module Decomposition

## Dependency Graph

config_loader -> catalog_filter
config_loader -> refusal_handler
catalog_filter -> mcp_proxy_core
refusal_handler -> mcp_proxy_core
logging -> mcp_proxy_core
mcp_proxy_core -> hindsight_mcp_backend

## Modules

### 1. config_loader
**Responsibility:** Load declarative configuration listing forbidden tool names.
**Inputs:** Config file / env var.
**Outputs:** Set of forbidden tool names.
**Dependencies:** None.
**Notes:** Validates exactly three names: clear_memories, delete_bank, clear_mental_model.

### 2. catalog_filter
**Responsibility:** Intercept tool catalog/list responses and remove forbidden tools.
**Inputs:** Catalog response from backend, forbidden set from config_loader.
**Outputs:** Filtered catalog.
**Dependencies:** config_loader
**Acceptance:** Filtered catalog excludes exactly three names, preserves order and metadata of others.

### 3. refusal_handler
**Responsibility:** Detect incoming calls to forbidden tools and return refusal.
**Inputs:** Request payload, forbidden set.
**Outputs:** Refusal error response or pass-through.
**Dependencies:** config_loader
**Acceptance:** Refusal for three tools only.

### 4. mcp_proxy_core
**Responsibility:** Core proxy that routes requests to Hindsight MCP, applies catalog_filter and refusal_handler.
**Inputs:** MCP messages.
**Outputs:** Responses.
**Dependencies:** catalog_filter, refusal_handler, logging
**Acceptance:** Transparent pass-through for allowed tools.

### 5. logging
**Responsibility:** Audit log refused calls and proxy events.
**Inputs:** Events from mcp_proxy_core.
**Outputs:** Logs.
**Dependencies:** None.

### 6. hindsight_mcp_backend
**Responsibility:** External Hindsight MCP service.
**Dependencies:** N/A

## Concurrency
Modules 2 and 3 can operate in parallel on independent requests. Catalog filter runs on list calls; refusal handler runs on call calls.

## Interfaces
- config_loader: returns Set<string>
- catalog_filter: (catalog, forbiddenSet) => catalog
- refusal_handler: (request, forbiddenSet) => {refuse | pass}
- mcp_proxy_core: handles JSON-RPC messages.

## Artifacts
- spec_manifest.md
- module_decomposition.md
- Config schema definition
