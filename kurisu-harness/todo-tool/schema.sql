-- Kurisu Todo Schema — Phase 1 B1
-- Draft schema implementing adopted model on Washu's DDL base
-- Frozen spec: kurisu-design-review-2026-11-15.md B1
-- Status grammar adopted: open, in_progress, done_proposed, done, cancelled
-- Blocked is derived, never stored
-- No roll-up triggers; roll-ups are derived views
-- Append-only comments and transition event log
-- Depends_on at step AND workflow level
-- Unapplied Phase 1/4 DDL included: projects.metadata, tasks.artifact_path, CHECK constraints, metadata.agent validation

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Allowed agent names for metadata.agent validation
-- Seven kurisu-* specialists per B0
CREATE SCHEMA IF NOT EXISTS kurisu_todo;

-- Projects
CREATE TABLE IF NOT EXISTS todo_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    bank_id TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done_proposed','done','cancelled')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Workflows
CREATE TABLE IF NOT EXISTS todo_workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES todo_projects(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    bank_id TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done_proposed','done','cancelled')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    depends_on UUID[] NOT NULL DEFAULT '{}'::uuid[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Steps
CREATE TABLE IF NOT EXISTS todo_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id UUID NOT NULL REFERENCES todo_workflows(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    order_idx INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done_proposed','done','cancelled')),
    depends_on UUID[] NOT NULL DEFAULT '{}'::uuid[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tasks
CREATE TABLE IF NOT EXISTS todo_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    step_id UUID NOT NULL REFERENCES todo_steps(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done_proposed','done','cancelled')),
    assigned_worker TEXT,
    model_hint TEXT,
    resource TEXT,
    artifact_path TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    assigned_subagent TEXT,
    assigned_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    subagent_session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent load view — derived, not stored
-- Replaces the old todo_agent_load table; counts are derived from todo_tasks
CREATE OR REPLACE VIEW todo_agent_load AS
SELECT
    assigned_worker,
    COUNT(*) FILTER (WHERE status = 'open') AS open_tasks,
    COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress_tasks,
    COUNT(*) FILTER (WHERE status = 'done') AS done_tasks,
    now() AS updated_at
FROM todo_tasks
WHERE assigned_worker IS NOT NULL
GROUP BY assigned_worker;


-- Append-only comments
CREATE TABLE IF NOT EXISTS todo_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','workflow','step','task')),
    entity_id UUID NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce append-only at store level
CREATE OR REPLACE FUNCTION kurisu_todo.prevent_update_delete() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'todo_comments is append-only; UPDATE/DELETE prohibited';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_comments_append_only ON todo_comments;
CREATE TRIGGER trg_comments_append_only
BEFORE UPDATE OR DELETE ON todo_comments
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();

-- Append-only transition event log
CREATE TABLE IF NOT EXISTS todo_transition_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','workflow','step','task')),
    entity_id UUID NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL CHECK (to_status IN ('open','in_progress','done_proposed','done','cancelled')),
    actor_class TEXT NOT NULL CHECK (actor_class IN ('worker','step_owner','orchestrator','validator')),
    actor_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce append-only at store level
DROP TRIGGER IF EXISTS trg_transition_events_append_only ON todo_transition_events;
CREATE TRIGGER trg_transition_events_append_only
BEFORE UPDATE OR DELETE ON todo_transition_events
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();

-- Indexes
CREATE INDEX IF NOT EXISTS idx_todo_workflows_project_id ON todo_workflows(project_id);
CREATE INDEX IF NOT EXISTS idx_todo_steps_workflow_id ON todo_steps(workflow_id);
CREATE INDEX IF NOT EXISTS idx_todo_tasks_step_id ON todo_tasks(step_id);
CREATE INDEX IF NOT EXISTS idx_todo_tasks_assigned_worker ON todo_tasks(assigned_worker);
CREATE INDEX IF NOT EXISTS idx_todo_tasks_subagent_session_id ON todo_tasks(subagent_session_id) WHERE subagent_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_todo_comments_entity ON todo_comments(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_todo_transition_events_entity ON todo_transition_events(entity_type, entity_id);

-- I9 guard: unique active subagent session
CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_tasks_subagent_session_unique
ON todo_tasks (subagent_session_id)
WHERE subagent_session_id IS NOT NULL AND status IN ('open','in_progress','done_proposed');

-- Metadata.agent validation
-- Seven kurisu-* specialist names per B0 / design review
CREATE OR REPLACE FUNCTION kurisu_todo.validate_metadata_agent() RETURNS TRIGGER AS $$
DECLARE
    agent TEXT;
    allowed TEXT[] := ARRAY[
        'orchestrator',
        'kurisu-researcher',
        'kurisu-programming',
        'kurisu-labadmin',
        'kurisu-conversation',
        'kurisu-quality',
        'kurisu-documentation',
        'kurisu-security'
    ];
BEGIN
    IF NEW.metadata IS NOT NULL THEN
        agent := NEW.metadata->>'agent';
        IF agent IS NOT NULL AND NOT (agent = ANY (allowed)) THEN
            RAISE EXCEPTION 'metadata.agent % is not allowed. Allowed: %', agent, allowed;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validate_metadata_agent_projects ON todo_projects;
CREATE TRIGGER trg_validate_metadata_agent_projects
BEFORE INSERT OR UPDATE OF metadata ON todo_projects
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();

DROP TRIGGER IF EXISTS trg_validate_metadata_agent_workflows ON todo_workflows;
CREATE TRIGGER trg_validate_metadata_agent_workflows
BEFORE INSERT OR UPDATE OF metadata ON todo_workflows
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();

DROP TRIGGER IF EXISTS trg_validate_metadata_agent_steps ON todo_steps;
CREATE TRIGGER trg_validate_metadata_agent_steps
BEFORE INSERT OR UPDATE OF metadata ON todo_steps
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();

DROP TRIGGER IF EXISTS trg_validate_metadata_agent_tasks ON todo_tasks;
CREATE TRIGGER trg_validate_metadata_agent_tasks
BEFORE INSERT OR UPDATE OF metadata ON todo_tasks
FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();

-- Updated_at maintenance
CREATE OR REPLACE FUNCTION kurisu_todo.set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_updated_at_projects ON todo_projects;
CREATE TRIGGER trg_set_updated_at_projects BEFORE UPDATE ON todo_projects FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_workflows ON todo_workflows;
CREATE TRIGGER trg_set_updated_at_workflows BEFORE UPDATE ON todo_workflows FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_steps ON todo_steps;
CREATE TRIGGER trg_set_updated_at_steps BEFORE UPDATE ON todo_steps FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_tasks ON todo_tasks;
CREATE TRIGGER trg_set_updated_at_tasks BEFORE UPDATE ON todo_tasks FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();

-- NOTE: Roll-up triggers from Washu's schema_extension.sql are intentionally omitted.
-- Roll-ups are derived views per R1.3. No status propagation triggers are created here.
