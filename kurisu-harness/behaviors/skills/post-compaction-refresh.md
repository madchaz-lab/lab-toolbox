# Post-Compaction Refresh Skill

## Purpose
Automate the post-compaction refresh path per AGENTS.md Rule 5.1, ensuring detection, verification, and record writing are reliable and repeatable. Lifecycle actions run through the todo tool's verbs. (The raw-HTTP token choreography is retired as a routine path; it survives only as the incident fallback in the annex skill post-compaction-refresh-http-fallback, invoked when the tool path itself is the incident, and its use is recorded on the board.)

## Detection
Primary detection is exact opencode v2 framing sentence:
`The following is a summary and serialized record of earlier conversation. Treat it as historical context, not as new instructions.`
Fallback: compaction summary present in context at start of turn.

## Procedure
1. Read AGENTS.md from disk `/home/makise-kurisu/AGENTS.md`. Compute sha256.
2. Re-read ~/skills/todo-tool.md in full - verb mechanics, credential classes, and the class-crossing recipe. A compaction drops them; board verbs called from summary memory refuse with "<class> required" or misroute. Do this before any board verb beyond reads. (Added 2026-11-22: the relaunched session hit compaction during its initial load and called propose_step_done through the tool surface twice before the skill was loaded by hand.)
3. Read board via `todo.board_view` and in-flight element transition event log `todo.list_events`.
4. Reflect on kurisu bank.
5. Recall specifics as needed.
6. Resume in-flight work by verification against store, never from summary.
7. Write refresh record as comment on the standing refresh-record work order (410bd2fd-29eb-471c-8624-5e745005c010) via `todo.append_comment` (worker class - from the orchestrator seat use the class-crossing recipe in ~/skills/todo-tool.md section 4, minting a worker token bound to this work order):
   - Comment body must contain:
     * Trigger name and timestamp, e.g., `auto-compaction at 2026-11-16T04:08:51Z`
     * `AGENTS.md sha256: <hash>`
     * Board state verified from store at time of writing
     * STATE DISPLAY – open work in order across projects (steps and work orders with statuses), closures owed, gates standing, workers running or paused – rendered from `todo.board_view` at time of refresh, derived from store, never from summary
     * GATES section quoted from the gates record (AGENTS.md section 8, standing-gates project on the board). A gate that cannot be found on the gates record is reported as NOT FOUND rather than silently omitted.
   - Verify comment exists in store before proceeding.

## Lifecycle verb order (through the tool)
1. `todo.start_work_order` – worker, work order must be open
2. `todo.set_artifact_path` – worker, before propose
3. `todo.propose_done` – worker, work order must be in_progress
4. `todo.accept_work_order` – step_owner, work order must be done_proposed
5. `todo.validate_done` – validator, work order must be accepted, artifact present
6. `todo.append_comment` – worker, closing summary (class-crossing recipe from the orchestrator seat)

Preconditions:
- start before propose
- artifact before validate
- accept before validate
- Validation must check record currency: comment must name THIS compaction's trigger timestamp, not just presence.

## Acceptance Criteria
- Refresh record comment exists on the refresh work order with correct trigger timestamp and current AGENTS.md sha256.
- Board state verified from store, not summary.
- No stale artifact accepted; validation checks currency.

## Notes
- Artifacts belong in `~/reports/`, not `/tmp/`.
- One comment per compaction on the refresh work order.
- Lifecycle verbs are called through the todo tool; class-crossing follows ~/skills/todo-tool.md section 4. The HTTP fallback annex exists for the one case where the tool path itself is the incident.
