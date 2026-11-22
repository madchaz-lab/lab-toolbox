# Hindsight - mechanics

The how for the hindsight MCP, as revised 2026-11-21 and made multi-bank
2026-11-22 (batch 10): one MCP entry per bank, every entry running through
the filtering proxy. Doctrine (when to recall / retain / reflect, who
may touch which bank, the sensitivity gate) lives in AGENTS.md sections 2
and 7. This skill is the surface and the choreography. Load it before any
bank work: closings, orientation, seeding, the post-compaction refresh path.

## The connection

- Your config carries one remote MCP entry per bank, and every entry
  runs through the filtering proxy (systemd user service
  kurisu-filtering-proxy on this VM), NOT the raw Hindsight service:
  hindsight -> /mcp/kurisu/, hindsight-researcher ->
  /mcp/kurisu-researcher/, and likewise hindsight-programming,
  hindsight-labadmin, hindsight-conversation, hindsight-quality,
  hindsight-documentation, hindsight-security; hindsight-chronoa ->
  /mcp/chronoa-singlebank/. All paths are on 127.0.0.1:8892. Never
  repoint a session at the raw address.
- Each entry is BOUND to its bank by its URL path. Every session you
  spawn inherits every entry. There is no bank argument on any verb and
  no bank switching: a call made through an entry lands in that entry's
  bank, whatever its tags say. Tags organize; they do not route. The
  entry IS the routing decision: a worker's spec names the bank and its
  entry, and a verb called through the wrong entry lands in the wrong
  bank exactly as surely as a misrouted retain ever did.
- Auth is the Authorization header opencode injects per entry (expanded
  from the environment at launch). You never handle, print, copy, or
  store the value - and bank work needs no credential handling at all.
- The proxy forwards ONLY /mcp/<bank>/ paths and refuses everything else
  (404). It removes three verbs from the catalog and refuses calls to
  them: clear_memories, delete_bank, clear_mental_model. They are not in
  the verb list below because they do not exist on this surface; a
  remembered procedure that calls one is stale - stop and use
  invalidate_memory (AGENTS.md 2.5). On the chronoa-singlebank path the
  proxy additionally refuses every WRITE verb (retain, sync_retain,
  update_memory, invalidate_memory, delete_document, mental-model and
  directive writes, page writes, update_bank): that path is read-only,
  mechanically (batch 10).

## Reaching another bank (bank-operation work)

Desk-bank work (seeding, desk-bank retains and recalls) is done by
instances of the bank-operation template calling the target bank's own
MCP entry: a run into kurisu-researcher calls the hindsight-researcher
entry's verbs, a run into kurisu-quality calls hindsight-quality's, and
so on. The worker's spec names the bank, its entry, and the access
allowed (AGENTS.md 2.4). There is no hand-rolled HTTP and no credential
step: the entry carries its auth in the config, and the worker never
fetches, sees, or writes a credential for bank work.

Verify a landing ON THE NAMED BANK, through its entry (get_bank,
list_documents, list_memories, recall on that entry) - the hindsight
entry's recall can only ever see kurisu, so it can never verify another
bank.

Incident fallback: if a session's tool surface lacks the entries (a
broken config load), the same proxy serves MCP over HTTP at
http://127.0.0.1:8892/mcp/<bank-id>/ and the work can proceed by that
path, with credentials from the named Kubernetes Secret at run time.
Record the fallback's use on the board - it is an incident path, not a
routine one.

## Orientation models (adopted 2026-11-22)

Mental models put to work, two ways:

- Desk orientation models. A seeding run into a desk bank closes by
  creating that desk's orientation model in the same bank - it is a step
  of the bank-operation template (v3, key orientation_model), not an
  optional extra. Mechanics: create_mental_model through the target
  bank's MCP entry, name "kurisu-<desk> orientation", a source_query
  that synthesizes the desk's craft (patterns, pitfalls, verified facts
  of the seat's work), trigger_refresh_after_consolidation set. Verify
  with get_mental_model through the same entry, and cite the model id in
  the run's closing comment. The desk's workers read it (get_mental_model)
  to orient before working the desk's queue.
- Lab state (bank kurisu, trial). One model in bank kurisu named
  "Lab state": nodes and their cards, production models and endpoints,
  VIPs, standing decisions of record. It serves orchestrator orientation;
  the post-compaction refresh path (AGENTS.md 5.1 steps 3-4) may read it
  first while the trial stands. Boundary: the refresh record's state
  display and gates section render from the board and the gates record -
  never from this model.

A mental model is synthesis, with a refresh timestamp (last_refreshed_at,
on list_mental_models): orientation, never evidence. Check its freshness
before leaning on it; a claim the store does not confirm is not done,
however the model phrases it.

## Verb catalog (live, verified 2026-11-22 through the proxy)

Core:
- retain(content, [context, timestamp, tags, metadata, document_id,
  strategy, update_mode]) - store, ASYNCHRONOUS (see Timing).
- sync_retain(content, [context, timestamp, tags, metadata, document_id,
  strategy]) - store and BLOCK until stored and recallable.
- recall(query, [max_tokens, budget, types, prefer_observations, tags,
  tags_match, tag_groups, query_timestamp, min_scores, temporal_window,
  rerank, include_chunks, include_entities, include_source_facts, ...]) -
  retrieve from the bound bank.
- reflect(query, [context, budget, max_tokens, response_schema, tags,
  tags_match, apply_all_directives, include_based_on, include_trace]) -
  synthesized answer over the bound bank (plan drafting, refresh path).

Memories:
- list_memories([type, q, limit, offset, tags, tags_match])
- get_memory(memory_id)
- update_memory(memory_id, [text, context, occurred_start, occurred_end,
  mentioned_at, fact_type, entities, resolve_entities])
- invalidate_memory(memory_id, [reason, restore]) - THE memory-removal
  verb: one at a time, with a reason (AGENTS.md 2.5).

Documents:
- list_documents([q, limit, offset])
- get_document(document_id)
- delete_document(document_id) - single-item delete; it is not one of the
  refused trio. Bank-level clearing is what does not exist here.

Mental models:
- list_mental_models([tags, limit, offset]) / get_mental_model(id,
  [detail]) / create_mental_model(name, source_query, [...]) /
  update_mental_model(id, [...]) / delete_mental_model(id) /
  refresh_mental_model(id)

Directives:
- list_directives([tags, active_only, limit, offset]) /
  create_directive(name, content, [priority, is_active, tags]) /
  delete_directive(id)

Operations (async work tracking):
- list_operations([status, type, limit, offset, exclude_parents])
- get_operation(operation_id)
- cancel_operation(operation_id)

Bank, tags, pages:
- get_bank() - the bound bank's id, name, disposition, mission.
- update_bank([name, mission, config_updates]) - bank config is
  madchaz's (AGENTS.md section 7); use only on his direction. There is no
  create-bank verb on this surface.
- list_tags([q, limit, offset])
- get_page_tree() / search_pages(query, [limit]) / get_page(page_id) /
  create_page_folder(name, [parent_id]) / create_page(name, source_query,
  [...]) / update_page_node(node_id, [...]) / delete_page_node(node_id)

## Timing - extraction is the funnel

retain is asynchronous: the call returns, and extraction runs afterwards
on db02's single Qwen3-8B, shared by every bank. Consequences:

- A retain is NOT recallable the moment the call returns. It lands when
  its operation completes: track it with list_operations / get_operation,
  and confirm with recall (or list_documents / list_memories) before
  reporting the retain as landed.
- sync_retain exists for verification-critical single items (a probe): it
  blocks until the item is stored and recallable. Prefer it over polling
  when the next step depends on the item being visible.
- Because every bank's extraction funnels through db02, seeding runs are
  SERIAL - one bank at a time, each verified landed in its named bank
  before the next begins (board rule, seeding-curation project). Parallel
  retains into several banks outrun the extractor and make landings
  unverifiable in order.

## Retain shape

- One canonical item per fact (AGENTS.md 2.2): conclusions and decisions,
  never raw material, never secret values, never copies of what a
  worker's bank or Postgres holds - retain a pointer (store, table, id).
- Document-shaped content (an issue, a seeded document, a report) rides
  retain with a document_id; update_mode governs re-retaining the same
  document.
- Record the retain's returned operation / document reference in the
  board closing comment that cites it (AGENTS.md 3.4) - the citation is
  how a later reader finds the landing.
- Craft vs world facts (AGENTS.md 6.7): a worker's craft lessons go to
  its own desk bank (by bank-operation routing, above); world facts reach
  bank kurisu only through the orchestrator.

## Failure shapes

- A call to a refused verb returns a deterministic JSON-RPC error from
  the proxy naming the refusal. That is the proxy working as designed -
  never route around it (no raw address, no alternate client).
- A write verb called through the hindsight-chronoa entry returns the
  proxy's read-only refusal. That path is read-only by construction;
  Chronoa's own workflow never passes through this proxy.
- 404 from the proxy means the path was not /mcp/<bank>/ - the proxy is
  not a general HTTP proxy for the Hindsight REST API.
- An empty recall is a result about the BOUND bank only. It says nothing
  about any other bank's contents.
