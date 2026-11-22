-- Ontology cutover migration — 2026-11-18 (Muse, at madchaz's direction)
-- Vocabulary: board "workflow" (instance) -> TASK; board "task" (leaf) -> WORK ORDER;
-- WORKFLOW becomes the template entity (new tables). Data and history preserved;
-- only labels change. Run inside one transaction, AFTER a full pg_dump backup.
-- The canonical fresh-install schema (schema-v2) is generated from the migrated
-- store (pg_dump --schema-only) so the file and the live schema cannot drift.

BEGIN;

-- 1. New entities: workflow templates (immutable versions) + migration log
CREATE TABLE IF NOT EXISTS todo_workflow_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS todo_workflow_template_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES todo_workflow_templates(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    step_dag JSONB NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'orchestrator',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (template_id, version)
);

CREATE TABLE IF NOT EXISTS todo_migrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT NOT NULL
);

-- 2. Suspend append-only enforcement + old entity_type CHECKs for the relabel
DROP TRIGGER IF EXISTS trg_comments_append_only ON todo_comments;
DROP TRIGGER IF EXISTS trg_transition_events_append_only ON todo_transition_events;
ALTER TABLE todo_comments DROP CONSTRAINT IF EXISTS todo_comments_entity_type_check;
ALTER TABLE todo_transition_events DROP CONSTRAINT IF EXISTS todo_transition_events_entity_type_check;

-- 3. Relabel history (order matters: leaf first, then instance)
UPDATE todo_comments SET entity_type = 'work_order' WHERE entity_type = 'task';
UPDATE todo_comments SET entity_type = 'task' WHERE entity_type = 'workflow';
UPDATE todo_transition_events SET entity_type = 'work_order' WHERE entity_type = 'task';
UPDATE todo_transition_events SET entity_type = 'task' WHERE entity_type = 'workflow';

-- 4. New entity_type CHECKs
ALTER TABLE todo_comments
    ADD CONSTRAINT todo_comments_entity_type_check
    CHECK (entity_type IN ('project','task','step','work_order','template'));
ALTER TABLE todo_transition_events
    ADD CONSTRAINT todo_transition_events_entity_type_check
    CHECK (entity_type IN ('project','task','step','work_order'));

-- 5. Table + column renames (leaf first to free the name "todo_tasks")
ALTER TABLE todo_tasks RENAME TO todo_work_orders;
ALTER TABLE todo_workflows RENAME TO todo_tasks;
ALTER TABLE todo_steps RENAME COLUMN workflow_id TO task_id;

-- 6. Tasks (instances) record their template source; NULL = ad-hoc
ALTER TABLE todo_tasks
    ADD COLUMN template_id UUID REFERENCES todo_workflow_templates(id) ON DELETE RESTRICT;
ALTER TABLE todo_tasks
    ADD COLUMN template_version INTEGER;

-- 7. Load view rebuilt over work orders (same view name, new meaning)
CREATE OR REPLACE VIEW todo_agent_load AS
SELECT
    assigned_worker,
    COUNT(*) FILTER (WHERE status = 'open') AS open_tasks,
    COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress_tasks,
    COUNT(*) FILTER (WHERE status = 'done') AS done_tasks,
    now() AS updated_at
FROM todo_work_orders
WHERE assigned_worker IS NOT NULL
GROUP BY assigned_worker;

-- 8. Restore append-only enforcement
CREATE TRIGGER trg_comments_append_only
BEFORE UPDATE OR DELETE ON todo_comments
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();

CREATE TRIGGER trg_transition_events_append_only
BEFORE UPDATE OR DELETE ON todo_transition_events
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();

-- 9. Record the migration in the store itself
INSERT INTO todo_migrations (description) VALUES
('2026-11-18 ontology cutover (madchaz definitions, built by Muse): todo_workflows renamed to todo_tasks (instances) with template_id/template_version added; todo_tasks renamed to todo_work_orders; todo_steps.workflow_id renamed to task_id; comments/events entity_type relabeled workflow->task, task->work_order; workflow template tables created; todo_agent_load rebuilt over work orders. Full pg_dump taken before applying.');

COMMIT;
