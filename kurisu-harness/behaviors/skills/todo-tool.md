# Todo Tool

Authority for the todo tool's verbs, credential classes, and dispatch mechanics. AGENTS.md carries the rules; this skill carries the mechanics. Read it before your first board action in any session.

## 1. What the board is

Project = a goal. Template = a workflow (versioned, in the registry). Task = one instance of a template. Step = a gate inside a task, owned by one agent. Work order = one delegation under a step: one worker, one session, one verifiable artifact. Stored statuses: open, in_progress, done_proposed, done, cancelled - done_proposed is the pre-validation state for ALL entity types (work orders, steps, tasks, projects), not only work orders.

Derived state (computed by board_view, never stored, never set it): an open entity reads "ready" when its depends_on are all done (or it has none), "waiting" while dependencies are unfinished, "blocked" when a dependency is cancelled. A parent's rollup reads "In progress" while any child is active or the children are mixed, "done" when every child is done, "open" when every child is open, and null for a leaf.

## 2. Your credential, and its limits

Every call is authenticated by a signed token whose CLASS is read from the token itself: orchestrator, step_owner, worker, validator, or executor. The HTTP header is only a carrier - and since 2026-11-22 the class-gated verbs also accept the token as a call argument (section 4), which is the standard way to perform class acts from your session. The executor class belongs to the VM-local todo executor service alone (it records probe verdicts via todo.record_probe); never mint an executor token for session work.

Your opencode todo MCP sends one fixed header, carrying an orchestrator token. The same token exists in two places - embedded literally in ~/.config/opencode/opencode.json and as a copy at ~/.secrets/kurisu-todo-orchestrator-token; they are identical today, and rotating one does NOT rotate the other. Consequences:

- Orchestrator verbs and all read verbs work directly through your tools.
- Worker, step_owner, and validator verbs called WITHOUT a token fail with "<class> required": your connection's header is orchestrator-class and cannot perform them. That error means class mismatch. It is not a bug, and retrying the same call the same way will never fix it. The fix is the token argument (section 4): mint the class token and pass it as `token` on the same call, through the same tool surface.
- A worker you dispatch inherits the same fixed header. Dispatching a worker does NOT give it worker class. The dispatch spec hands the worker a token minted for its work order; the worker passes it as the `token` argument on its worker-verb calls (section 4).
- Acceptance and validation are YOUR legs, performed through the token argument (section 4) in your own turn: mint the step_owner token bound to the step's owner agent and call accept_work_order with it as `token`; mint a validator token and call validate_done the same way. Never package acceptance or validation as a work order for a dispatched worker - a worker cannot hold step_owner or validator class for someone else's order, and the attempt burns a dispatch before failing with "step_owner required" (15cf1e25, 2026-11-21). EXCEPTION (AGENTS.md 3.4, landed 2026-11-21): for tasks of the programming-build template, validation is performed by a validator SESSION, not by your own minted-token call - dispatch the validator agent with a spec naming the artifact's deployed location; the validator confirms on the board that the task's audit step has passed before validating an implement step's orders. Acceptance remains your leg in all cases.
- A "<class> required" error - yours, or reported back by a worker - is a routing correction, not a blocker: re-issue the same verb with the correctly minted token as the `token` argument, in the same turn. It never justifies a breakpoint or a halt; no external party, permission, or information is missing.

## 3. Verb table (complete - any other name returns Unknown tool)

Orchestrator class:

- todo.create_template(name, step_dag, description, metadata?) - register a workflow template (step_dag schema below); metadata is stored on version 1
- todo.add_template_version(name, step_dag, metadata?) - add a version; metadata replaces the version metadata. A version that DROPS a step key while live (non-terminal) instances of the template exist is refused by the destructive guard
- todo.instantiate_task(project_id, template_name, name, version?, bank_id?) - create a task from a template; template_name is the template NAME, not its id; returns {"id", "template", "template_version", "steps": {step key: step id}}
- todo.create_project(name, description?, bank_id?)
- todo.create_task_instance(project_id, name, bank_id?, depends_on?) - a task without a template
- todo.create_step(task_id, name, order_idx?, depends_on?, agent?, key?) - agent is optional and sets the step's owner inline; key is optional and stamps the step with a template-style key in its metadata (the recovery path for a cancelled audit step: create the replacement with key='audit'; a key already held by a live step of the task is refused); on a templated task, adding a step records a deviation in the task's metadata
- todo.set_step_owner(step_id, agent, token?) - agent must be one of the allowlisted names (section 3, step_dag owners)
- todo.cancel_element(entity_type, entity_id, reason, expected_revision?, force?) - cancels ONE element; it does NOT cascade to children, and it refuses an element already done or cancelled; cancelling a step of a templated task records a deviation. The destructive guard refuses a cancel with ACTIVE descendants (in_progress or done_proposed) unless force=true is passed - a forced cancel is recorded; use it only deliberately, never as a routine shortcut. The same guard also refuses cancelling the AUDIT step of an audit_required task while sibling steps are live (it would deadlock the task's validations); force=true overrides, and a replacement audit step can be created with create_step(key='audit')
- todo.propose_done_element(entity_type, entity_id) - propose a TASK or PROJECT done (never a step; steps use propose_step_done). Every child must be done or cancelled, and an element with no children is refused - cancel an empty element instead of proposing it
- todo.mint_token(token_cls, binding) - mint a class token (section 4); returns {"token", "token_hash", "token_cls", "expires_at"} - read expires_at from the response instead of assuming the TTL
- todo.request_slot(work_order_id, mode?, node?, session_id?) - the dispatch slot grant (section 6). mode "dispatch" (default): the work order must be open and its step's dependencies done; returns {"grant_id", "grant", "node", "provider"} for the first free node in db01, db03, db04 order, or {"slot": null, "reason"} when every node is granted out. The grant_id string at the top level is what bind_slot and release_slot take - "grant" is the full ledger row, for reading. mode "resume": the work order must be in_progress; pass the paused session's own node and session_id; granted only if that node is free
- todo.bind_slot(grant_id, session_id) - bind a held grant to the spawned child session; idempotent for the same session, refused for a different one. Binding also stamps the work order itself (subagent_session_id, assigned_at), so the serving session is readable from the order and board_view, not only from the grant ledger
- todo.release_slot(grant_id, reason) - release a held or bound grant (e.g. a granted dispatch abandoned before spawning)
- todo.list_grants(state?) - the grant ledger (newest first, latest 100; pass state to filter). Through the raw-HTTP fallback client, an EMPTY filtered result surfaces as {"error": "no result"} - that means no grants match, not a failure
- todo.render_composition(task_id) - one-call composition view of a task: its template stamp and metadata (incl. audit_required), per-step key/owner/status/template_step, deviations, and the task revision. Use it at the plan gate instead of assembling the diff from separate reads

The step_dag schema (create_template / add_template_version; instantiate_task re-validates it): a non-empty list of step objects, each {"key", "name", "owner", "depends_on", "acceptance"} - key: unique string within the DAG; name: required; owner: required, one of orchestrator, kurisu-researcher, kurisu-programming, kurisu-labadmin, kurisu-conversation, kurisu-quality, kurisu-documentation, kurisu-security; depends_on: list of other steps' keys (may be empty); acceptance: required, non-empty - a step is a gate with one acceptance check. The DAG must be acyclic.

Reads (no class check):

- todo.board_view(project_id?) - returns {"projects": [...]}; work orders carry "name", not "title"; step nodes carry "agent" (owner), "key" (the template key, e.g. "audit"), depends_on and order_idx; work-order nodes carry assigned_worker, artifact_path, subagent_session_id, accepted_by and the probe records. Pass project_id to return only that project: the unfiltered call returns every project on the board (38 and growing) and costs accordingly - prefer the filtered call for project work and refreshes
- todo.agent_load() - per-worker counts from the load view: open_tasks, in_progress_tasks, done_proposed_tasks (awaiting acceptance/validation - the closure queue), done_tasks. Orders with no assigned_worker appear as a row with assigned_worker null; no order is invisible here
- todo.list_templates() / todo.get_template(name, version)
- todo.list_projects() / todo.list_tasks(project_id) / todo.list_steps(task_id) / todo.list_work_orders(step_id) - the filter argument is optional. list_steps returns the step's order_idx, agent (owner), key (template key) and depends_on. list_work_orders returns assigned_worker, artifact_path, subagent_session_id, accepted_by, and the executor's probe_result / probe_confirm records as they land. Task, step, and work-order rows carry a "revision" counter (starts at 1, bumped by every status-changing write); board_view nodes carry the same fields
- todo.get_work_order(work_order_id) - the full detail for one work order: every column plus the complete metadata (accepted_by, the structured completion, the probe records). The list reads stay lean; when a decision needs the metadata, read it here - never conclude a fact is absent from the store because a list read does not show it
- todo.get_task(task_id) / todo.get_step(step_id) - the same detail view for a task or a step: full row with metadata parsed (a task's metadata carries its template stamp and recorded deviations; a step's carries its owner and template key) and depends_on normalized
- todo.list_comments(entity_type, entity_id, limit?) / todo.list_events(entity_type, entity_id) - limit returns the most recent N comments in chronological order; use it on long-lived orders (the standing refresh-record order) instead of reading the whole history

Worker class (token bound to the work order):

- todo.start_work_order(work_order_id, token?) - order must be open and the STEP's depends_on done (dependencies gate the step, not the order); moves the order to in_progress, and the step too if it was still open
- todo.set_artifact_path(work_order_id, artifact_path, token?) - before propose_done; validation refuses (and bounces) an order with no artifact_path
- todo.propose_done(work_order_id, comment?, artifact_kind, deployed_location, background_work, probe?, probe_id?, expected_revision?, token?) - order must be in_progress; lands done_proposed. The STRUCTURED completion is the only shape (WS1): artifact_kind is one of code, document, config, service, data, none; deployed_location is a non-empty string naming where the artifact actually runs or lives; background_work lists anything the worker left running (an empty list is a claim that nothing was left); an unstructured proposal is refused. Pass the closing summary as `comment`: it is recorded as a worker comment in the same call. The closing comment ALWAYS rides propose_done - never plan a separate append_comment after validation, when the token may no longer be alive (breakpoint e78fbac8, fixed 2026-11-20). When the plan declared an acceptance probe, pass it as `probe` ({"id", "kind", ...spec} - kinds: file-hash, file-exists, grep-count, http-get, sql-select, cli-gate, mcp-call): the VM-local executor grades it after the proposal (verdict) and again after acceptance (confirmation), and the probe guard refuses validation until both records exist with identical digests (section 5)
- todo.append_comment(entity_type, entity_id, body, token?) - entity_type must be "work_order" and the id must be the token's bound order; a worker can comment on nothing else. For mid-flight notes (e.g. section 6.10 gate records), not for the closing summary

Step_owner class (the token's agent must equal the step's owner):

- todo.create_work_order(step_id, name, assigned_worker?, token?) - assigned_worker is OPTIONAL and informational only: it is never consulted for authorization and grants nothing - a worker's authority is its token binding alone, and an order with no assigned_worker is fully workable (never cancel or recreate an order over a missing assigned_worker). There is no step-status precondition: an order can be created under a step in any status, and a late order created under a done_proposed step blocks that step's validation until it is done or cancelled
- todo.accept_work_order(work_order_id, token?) - order must be done_proposed; acceptance is RECORDED (accepted_by in metadata) but the status does not change - the order stays done_proposed until the validator moves it. Acceptance is readable: accepted_by rides list_work_orders and board_view, and todo.get_work_order returns the full metadata record. todo.list_events remains the transition proof (a step_owner done_proposed -> done_proposed event whose actor_id is the accepting agent). (An earlier version of this skill said no read verb returns acceptance - that was true until the 2026-11-22 read fixes; do not reason from it.)
- todo.reject_work_order(work_order_id, reason, token?) - order must be done_proposed; sends the order BACK to in_progress for rework
- todo.cancel_work_order(work_order_id, reason, token?) - refuses an order already done or cancelled
- todo.propose_step_done(step_id, reason, token?) - two paths: (a) the step has work orders: every order must be done or cancelled AND the step must be in_progress; (b) the step has NO work orders: reason is required and is recorded on the step as the closure justification. Corner: a step still open whose orders were all cancelled before any start fails path (a)'s in_progress precondition - close it with cancel_element instead

Executor class (the VM-local todo executor alone; never a session verb, never mint it for session work):

- todo.record_probe(work_order_id, probe_id, digest, value, completion_at, phase) - records the executor's probe verdict (phase "verdict") and post-acceptance confirmation (phase "confirm") on a work order's metadata. Listed so the table is complete: your sessions read the results via todo.get_work_order, you never call this

Validator class:

- todo.validate_done(entity_type, entity_id, expected_revision?, token?) - the only path to done; done is validator-exclusive. The entity must be done_proposed. For a work order it additionally requires acceptance recorded AND artifact_path set: if either is missing, validation does not merely fail - the order is BOUNCED back to in_progress with a "validation failed" comment. For every entity type, validate_done also re-checks dependencies: an entity whose depends_on are not all done is refused ("dependencies not done"). GUARDS run before the lifecycle checks (WS4, deny-only; every denial is recorded in todo_guard_denials and as a comment on the entity): the PROBE guard (work orders whose completion declares a probe: executor verdict + post-acceptance confirmation with identical digests, both keyed to that completion - a missing or mismatched record refuses validation; wait for the executor, or treat a digest change as the artifact having changed after acceptance); the AUDIT guard (steps of an audit_required task: no step validates before the task's audit step is done - the step waits in done_proposed while the audit runs; that is the choreography, not a stall). A guard refusal is the tool working: fix the cause or wait for the missing record; never route around a guard

### Specs on work orders (the plan-gate pattern)

A step whose plan requires reading a spec comment from its work order ("read the spec comment before planning") depends on a comment that creation cannot attach:

- todo.create_work_order has no body or comment parameter - a work order is born with zero comments.
- todo.append_comment is worker-class ONLY. No other class can comment on a work order - not even the orchestrator. (The orchestrator / step_owner / validator comments visible on orders are written internally by the cancel, reject, and validate paths, not by append_comment.)

So the spec gets onto the order in a deliberate second step, BEFORE the reader is dispatched: mint a worker token bound to the new work order (section 4) and append_comment the spec with that token as the `token` argument. Then verify with todo.list_comments - the spec must be returned before any worker is dispatched to read it. A spec-gated order dispatched with an empty comment list is a creation defect that surfaces as a mid-flight breakpoint (813076c1, 66a8321a); catch it at creation instead.

## 4. Class-crossing: the token argument (the standard path for non-orchestrator verbs)

The class-gated verbs - create_work_order, accept_work_order, reject_work_order, cancel_work_order, propose_step_done, set_step_owner (step_owner); start_work_order, set_artifact_path, propose_done, append_comment (worker); validate_done (validator) - accept an optional `token` argument. The server verifies that token and performs the call under ITS class and binding. Your connection's header stays orchestrator throughout; the token argument is how class acts are performed from your session - no second connection, no raw HTTP, no client code.

1. Mint: call todo.mint_token through your tools (an orchestrator verb) with token_cls and binding:
   - worker: {"work_order_id": "<id>"} - lives 4 hours
   - step_owner: {"agent": "<the step owner's agent name>"} - lives 60 minutes
   - validator: {} - lives 60 minutes
2. Call the verb through your tools, passing the minted token as the `token` argument. Worked example (verified 2026-11-22): mint step_owner {"agent": "orchestrator"}, then todo.create_work_order(step_id, name, token=<minted>) creates the order under step_owner class on the same surface.
3. Rules of the path:
   - Fail-closed: a supplied token decides alone. An invalid token returns "invalid token: ..."; an expired one returns "invalid token: token expired". The call does NOT fall back to your orchestrator identity - re-mint and retry.
   - Bindings still apply after class resolves: a worker token acts only on its bound work order; a step_owner token only on steps whose recorded owner matches its bound agent.
   - With no token argument, the connection header decides, exactly as before - a class-gated verb called without one still returns "<class> required".
   - The transition log records the acting class from the token: closures still read worker proposed / step_owner accepted / validator validated.
   - TTLs: worker 4 h, step_owner/validator 60 min. Read expires_at from the mint response instead of assuming.
   - Never mint an executor token for session work; todo.record_probe belongs to the VM-local executor service alone.

### Fallback: raw HTTP (out-of-session actors and tool-surface incidents only)

The raw protocol remains for actors with no tool surface of their own, and for the incident path in the post-compaction-refresh skill. Facts: the endpoint is http://kurisu-todo.brains-containment.svc.cluster.local:8001/mcp and the MCP is stateful - POST "initialize" (protocolVersion "2025-03-26"), read Mcp-Session-Id from the RESPONSE HEADER, POST "notifications/initialized", send that header on every request. The server reads exactly three token headers in priority order - x-worker-token, x-orchestrator-token, x-validator-token; the FIRST present is the only one verified, and the class comes from the token payload, not the header name. Authorization: Bearer is never read. Send exactly one token header per request. A complete working client is ~/class_crossing_recipe.py - IMPORT it; never hand-roll a client inline (hand-rolled clients are the 2026-11-20 failure family: tokens minted and never presented, wrong headers, skipped handshakes).

Bootstrap: if the orchestrator token has expired, mint fresh tokens from the token-secret in Secret kurisu-todo-creds (key token-secret) using the HMAC recipe in ~/skills/post-compaction-refresh-http-fallback.md.

## 5. Lifecycle order

Work order: create_work_order (step_owner) -> request_slot + dispatch (section 6) -> start_work_order (worker) -> set_artifact_path (worker) -> propose_done (worker, structured completion + closing summary in the comment parameter) -> accept_work_order or reject_work_order (step_owner) -> validate_done (validator). Reject loops back to in_progress; accept only records acceptance. append_comment (worker) attaches mid-flight notes to its own order while its token lives. If the completion declares a probe, the executor's verdict lands after propose_done and its confirmation after accept_work_order (each within about a minute); validation before both are recorded is refused by the probe guard - wait and retry. Probe state is readable as it lands via todo.get_work_order (probe_result, then probe_confirm) - check there instead of validating blind.

Revisions: lifecycle verbs accept expected_revision. When a closure must not land on a state you did not read (shared elements, contested orders), pass the revision from your read; a stale value is refused with "revision mismatch" - re-read and decide again. Revisions are optimistic concurrency, not a lock.

Step closure: propose_step_done (step_owner) -> validate_done (validator). There is no orchestrator propose step for steps - propose_done_element covers tasks and projects only.

Task / project closure: propose_done_element (orchestrator) -> validate_done (validator). Closure is per-element: children reaching done never move the parent - a task whose steps are all done still reads open until its own propose + validate pair lands, and the same for a project over its tasks. Report an element closed only from its own status.

Closure responses carry next_ready (batch 8): validate_done, propose_done_element and propose_step_done return the board's first dispatch-ready work order - the oldest open order whose step dependencies are done and which holds no active grant, under a live step/task/project - with ready_count and free_nodes, or null when nothing is dispatch-ready. It is computed after the closure lands, through the same gate request_slot applies. AGENTS.md 6.8 gives it its force: work it, or cite its block, in the same turn.
Batch 9: when no work order is dispatch-ready, next_ready falls back to the oldest ready step holding no live work order - step open/in_progress, its dependencies done, task/project live, no order in open/in_progress/done_proposed under it - returned as kind "step" with action "create_work_order", step_id, step_name, step_owner, task/project names, ready_count (of ready steps) and free_nodes. The act it names is creating that step's work order in the same turn. A work-order item carries kind "work_order". next_ready is null only when neither an order nor a step is ready.

Dependencies gate twice: a step whose depends_on is not done refuses every start under it, and validate_done refuses any entity whose depends_on is not done. The propose verbs do not check dependencies.

## 6. Dispatch and the cap (mechanics; the rule itself is AGENTS.md section 4)

The slot is granted by the tool (WS2, shipped 2026-11-22). There is no manual node check anymore - the grant ledger is the mechanism, and the reconciler (a VM-local timer) releases grants whose sessions finish or go quiet for 30 minutes, and flags running sessions with no bound grant. There is NO task timeout anywhere in this stack: nothing kills, pauses, or deadlines a running worker - the reconciler only releases the slot reservations of sessions that already finished or went quiet, and a resume simply requests a fresh grant. Long work on this hardware runs to completion. Two spawn disciplines ride on the grant: the harness's built-in "general" subagent is never a dispatch target - every spawn is a kurisu-* seat (or the validator) bound to a grant for a board work order - and session housekeeping (AGENTS.md 4.0a: renaming your own session, your own session's administration) is never dispatched at all; it is done inline, in one call, and never given a work order or a grant.

Before EVERY dispatch:

1. Task first: the work order id is in hand. No id, no dispatch.
2. Request the grant: todo.request_slot(work_order_id). The tool checks the work order (open; its step's dependencies done; no grant already held for it) and returns the first free node in db01, db03, db04 order with its provider (glimmer-db01 / glimmer-db03 / glimmer-db04), or {"slot": null, "reason"} - a null slot is an answer, not an error: the work waits on the board.
3. Spawn the worker on the granted node, naming its model explicitly (the provider the grant returned). Agent defaults herd same-agent batches onto one node; the grant, not the default, decides placement.
4. Bind immediately: todo.bind_slot(grant_id, child session id) as soon as the spawn returns its session id. An unbound grant expires after 10 minutes, so do not batch binds. If the spawn fails or the dispatch is abandoned, todo.release_slot(grant_id, reason).

Resuming a paused worker is a dispatch (AGENTS.md 4.2b): todo.request_slot(work_order_id, mode="resume", node=<the session's own node>, session_id=<the session id>). The resume is granted only on the session's own node and only while that node is free. A paused worker whose grant was released by the reconciler after 30 quiet minutes is normal - request a fresh grant.

READING DEPENDENCIES (2026-11-22): list_steps, list_tasks and board_view return each element's depends_on. Read it; never infer dependencies from list order or order_idx - presentation order is not a dependency chain. Where readiness is in question, request_slot is the mechanical check: it refuses when a step's dependencies are unmet, and a returned grant is proof they are done. Do not sit serial on an assumed chain: when two ready steps' depends_on are both satisfied, both are dispatchable, subject to the cap.

Inspecting placement: todo.list_grants() shows held/bound grants (node, session, work order). Report placements from the ledger cross-checked against the session store, never from assignment intent or agent_load.

## 7. Error decoder

- "<class> required" - your token's class does not match the verb. Section 4. Never retry as-is.
- "step not owned by this agent" - your step_owner token's agent is not the step's owner. A binding mismatch, not a class mismatch: read the owner from board_view (the step's "agent" field) and mint for THAT agent.
- "Unknown tool" - wrong verb name. Section 3 is the whole surface (todo.create_task does not exist; the verb is todo.create_task_instance).
- "step dependencies not done" - a start was attempted under a step whose depends_on is unfinished. "dependencies not done" is the same gate at validation time.
- "status precondition failed: expected X, got Y" / "cannot propose done from status X" / "not proposed" - the lifecycle is stateful; read the entity (list_events, list_work_orders) before acting. A work order that was never started sits in open and refuses propose_done with "expected in_progress, got open" - start it first (todo.start_work_order).
- "validation failed: acceptance not recorded" / "validation failed: artifact missing" - validate_done bounced the work order back to in_progress: acceptance (step_owner) or set_artifact_path (worker) was skipped. Fix the missing step, re-propose, re-accept if needed, validate again.
- "not all work orders done or cancelled" / "not all children done or cancelled" - a propose was attempted with live children underneath.
- "element has no children; cancellation is the honest close for an empty element" - propose_done_element on an empty task/project; cancel it instead.
- "cannot cancel ... in status done/cancelled" - terminal elements cannot be cancelled.
- "depends_on references missing ids" - a depends_on id does not exist in the same entity table; dependencies are same-type only.
- "step key 'X' already exists on a live step of this task" - create_step key uniqueness: a cancelled step does not hold its key, a live one does. For audit-step recovery, the cancelled original frees the key automatically.
- "worker can only comment on work orders" - worker-class comments attach to the bound work order only.
- "worker bound to different work order" - your worker token resolved, but its binding names a different work order. A binding mismatch, not a class mismatch: use a token minted for THIS order. (The step_owner analogue is "step not owned by this agent".)
- "step validation failed: not all work orders done or cancelled" - validate_done on a step re-checks its orders and found a live one - possibly an order created after propose_step_done (create_work_order has no step-status precondition). List the step's orders, close or cancel the stray, validate again.
- "guard denied [probe]: ..." - validation was attempted before the executor's verdict + confirmation for this completion were both recorded, or their digests differ (the artifact changed after acceptance). Wait for the executor (about a minute per phase) and retry; a digest mismatch is a finding, not a glitch - re-propose against the current artifact.
- "guard denied [audit]: ..." - a step of an audit_required task was validated before its audit step validated done. The step waits in done_proposed; run the audit. If the message says the audit step is CANCELLED, no audit step can ever complete: create a replacement with todo.create_step(key='audit'), work it, and validate it - then this step can validate.
- "guard denied [destructive]: ..." - a cancel with active descendants (pass force=true only deliberately), cancelling the audit step of an audit_required task with live siblings, or a template version dropping a step key with live instances. Every denial is recorded in todo_guard_denials.
- "revision mismatch: expected N, entity is at M" - your expected_revision was stale; re-read the entity and decide again.
- "structured propose_done requires: artifact_kind ..." - the WS1 structured fields (artifact_kind, deployed_location, background_work) were missing or invalid; there is no unstructured shape.
- "work order already holds an active grant" / bind errors - read todo.list_grants() to see the ledger before retrying; a grant held for a dead dispatch is released with todo.release_slot.
- "dispatch requires an open work order, got X" / "resume requires an in_progress work order, got X" - request_slot state preconditions: dispatch grants only open orders, resume grants only in_progress ones. Read the order and fix its state first; a null slot (all nodes occupied) is a different answer and is NOT an error.
- "propose_done_element covers task and project; steps use propose_step_done" - the verb name sounds generic; steps close through propose_step_done (step_owner) only.
