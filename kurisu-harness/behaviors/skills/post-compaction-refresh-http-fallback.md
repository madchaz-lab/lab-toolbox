# Post-Compaction Refresh — HTTP Fallback (INCIDENT ANNEX)

NOTE (2026-11-18): the standard class-crossing path now lives in ~/skills/todo-tool.md section 4. This annex remains the bootstrap reference (minting from the token-secret) for when the tool path itself is the incident. Two v2-era errors in the text below are corrected: the worker binding is {"work_order_id": "<id>"}, and the verbs are start_work_order / accept_work_order.

**Incident fallback only.** This is the retired raw-HTTP token choreography for
the todo lifecycle. It is NOT a routine path: the todo tool's verbs are the
routine path (see the post-compaction-refresh skill). Invoke this annex only
when the tool path itself is the incident, and record its use on the board the
same turn.

Preserved verbatim from the pre-2026-11-18 refresh skill so the knowledge is
not lost.

## Todo Lifecycle over HTTP Choreography
Class tokens minted via secret `kurisu-todo-creds` secret `token-secret`.

Token minting:
```python
payload = {"cls": cls, "binding": binding, "exp": int(time.time())+ttl}
```
Sign with HMAC SHA256.

Header mapping:
- `X-Orchestrator-Token` for orchestrator class
- `X-Worker-Token` for worker class
- `X-Validator-Token` for validator class

Binding rules:
- worker: `{"work_order_id": "<id>"}`
- step_owner: `{"agent": "orchestrator"}` or `{"step_id": "<id>"}`
- validator: `{}`

Verb order and preconditions:
1. `todo.start_work_order` – worker, work order must be open
2. `todo.set_artifact_path` – worker, before propose
3. `todo.propose_done` – worker, work order must be in_progress
4. `todo.accept_work_order` – step_owner, work order must be done_proposed
5. `todo.validate_done` – validator, task must be accepted, artifact present
6. `todo.append_comment` – worker, after validation for closing summary
