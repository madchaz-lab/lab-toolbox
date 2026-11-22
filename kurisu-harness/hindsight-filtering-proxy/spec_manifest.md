# Filtering Proxy Spec Manifest

## Project
Project filtering-proxy 50edf8dd-d341-41aa-a76e-f1113422944e
Task filtering-proxy-build db4e8726-58ee-4467-8815-3bc140af3607
Step plan filtering proxy 23014317-6a26-4a33-b220-d07615914b98

## Overview
Create a filtering proxy that sits in front of Hindsight MCP, strips clear_memories, delete_bank, clear_mental_model from tool catalog and refuses those calls - exactly those three, nothing else filtered.

## Requirements

### Functional
1. Proxy must intercept all MCP requests to Hindsight.
2. Tool catalog/list operations must filter out exactly three tools:
   - clear_memories
   - delete_bank
   - clear_mental_model
3. No other tools may be filtered or altered.
4. Calls to the three forbidden tools must be refused with a deterministic error response.
5. All other Hindsight tool calls must pass through unchanged, preserving arguments, results, and metadata.
6. Proxy must be transparent for non-tool operations: resources, prompts, etc.

### Non-Functional
1. Minimal latency overhead.
2. Configuration must be declarative: list of forbidden tool names.
3. Audit logging for refused calls.
4. No secret values stored; reference by name only.

## Acceptance Criteria
1. Catalog listing returns tool list without clear_memories, delete_bank, clear_mental_model.
2. Catalog listing contains all other Hindsight tools unchanged.
3. Attempt to call clear_memories returns refusal error; same for delete_bank and clear_mental_model.
4. Attempt to call any other tool succeeds and returns original Hindsight response.
5. Proxy configuration contains exactly the three forbidden names, no more.
6. Static diagnostics pass: shellcheck for scripts, kubeconform for manifests if any.
7. Spec manifest and module decomposition artifacts exist and are referenced.

## Out of Scope
- No filtering of banks, resources, or other MCP capabilities.
- No modification of Hindsight internals.
- No authorization changes beyond refusal.

## Risks
- Over-filtering: must ensure only three tools removed.
- Under-filtering: tool name variants or aliases.
