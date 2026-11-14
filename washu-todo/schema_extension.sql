-- washu-todo schema extensions for assign_to_subagent, agent-load, status propagation
-- Run against washu-postgres public schema

-- 1. Extend todo_agent_load
ALTER TABLE todo_agent_load
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Ensure primary key
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'todo_agent_load_pkey'
  ) THEN
    ALTER TABLE todo_agent_load ADD PRIMARY KEY (assigned_worker);
  END IF;
END $$;

-- 2. Extend todo_tasks for subagent assignment and lifecycle
ALTER TABLE todo_tasks
  ADD COLUMN IF NOT EXISTS assigned_subagent TEXT,
  ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS subagent_session_id TEXT;

-- 3. Helpful indexes
CREATE INDEX IF NOT EXISTS idx_todo_tasks_assigned_worker ON todo_tasks(assigned_worker);
CREATE INDEX IF NOT EXISTS idx_todo_tasks_assigned_subagent ON todo_tasks(assigned_subagent);
CREATE INDEX IF NOT EXISTS idx_todo_tasks_step_id ON todo_tasks(step_id);
CREATE INDEX IF NOT EXISTS idx_todo_steps_workflow_id ON todo_steps(workflow_id);
CREATE INDEX IF NOT EXISTS idx_todo_workflows_project_id ON todo_workflows(project_id);

-- 4. Status propagation functions
CREATE OR REPLACE FUNCTION propagate_step_status() RETURNS TRIGGER AS $$
DECLARE
  step_id UUID;
  task_count INT;
  done_count INT;
  inprog_count INT;
  pending_count INT;
BEGIN
  step_id := COALESCE(NEW.step_id, OLD.step_id);
  SELECT COUNT(*), COUNT(*) FILTER (WHERE status='done'), COUNT(*) FILTER (WHERE status='in_progress'), COUNT(*) FILTER (WHERE status='pending')
  INTO task_count, done_count, inprog_count, pending_count
  FROM todo_tasks WHERE step_id = step_id;

  IF task_count = 0 THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  IF done_count = task_count THEN
    UPDATE todo_steps SET status='done', updated_at=now() WHERE id = step_id;
  ELSIF inprog_count > 0 THEN
    UPDATE todo_steps SET status='in_progress', updated_at=now() WHERE id = step_id;
  ELSIF pending_count > 0 THEN
    UPDATE todo_steps SET status='pending', updated_at=now() WHERE id = step_id;
  ELSE
    UPDATE todo_steps SET status='planned', updated_at=now() WHERE id = step_id;
  END IF;

  PERFORM propagate_workflow_status((SELECT workflow_id FROM todo_steps WHERE id = step_id));
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION propagate_workflow_status(workflow_uuid UUID) RETURNS VOID AS $$
DECLARE
  step_count INT;
  done_count INT;
  inprog_count INT;
BEGIN
  SELECT COUNT(*), COUNT(*) FILTER (WHERE status='done'), COUNT(*) FILTER (WHERE status='in_progress')
  INTO step_count, done_count, inprog_count
  FROM todo_steps WHERE workflow_id = workflow_uuid;

  IF step_count = 0 THEN
    RETURN;
  END IF;

  IF done_count = step_count THEN
    UPDATE todo_workflows SET status='done', updated_at=now() WHERE id = workflow_uuid;
  ELSIF inprog_count > 0 THEN
    UPDATE todo_workflows SET status='in_progress', updated_at=now() WHERE id = workflow_uuid;
  ELSIF done_count = 0 THEN
    UPDATE todo_workflows SET status='planned', updated_at=now() WHERE id = workflow_uuid;
  ELSE
    UPDATE todo_workflows SET status='pending', updated_at=now() WHERE id = workflow_uuid;
  END IF;

  PERFORM propagate_project_status((SELECT project_id FROM todo_workflows WHERE id = workflow_uuid));
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION propagate_project_status(project_uuid UUID) RETURNS VOID AS $$
DECLARE
  wf_count INT;
  done_count INT;
  inprog_count INT;
BEGIN
  SELECT COUNT(*), COUNT(*) FILTER (WHERE status='done'), COUNT(*) FILTER (WHERE status='in_progress')
  INTO wf_count, done_count, inprog_count
  FROM todo_workflows WHERE project_id = project_uuid;

  IF wf_count = 0 THEN
    RETURN;
  END IF;

  IF done_count = wf_count THEN
    UPDATE todo_projects SET status='done', updated_at=now() WHERE id = project_uuid;
  ELSIF inprog_count > 0 THEN
    UPDATE todo_projects SET status='in_progress', updated_at=now() WHERE id = project_uuid;
  ELSIF done_count = 0 THEN
    UPDATE todo_projects SET status='planned', updated_at=now() WHERE id = project_uuid;
  ELSE
    UPDATE todo_projects SET status='pending', updated_at=now() WHERE id = project_uuid;
  END IF;
END;
$$ LANGUAGE plpgsql;

-- 5. Triggers for propagation
DROP TRIGGER IF EXISTS trg_todo_tasks_status ON todo_tasks;
CREATE TRIGGER trg_todo_tasks_status
AFTER INSERT OR UPDATE OF status ON todo_tasks
FOR EACH ROW EXECUTE FUNCTION propagate_step_status();

DROP TRIGGER IF EXISTS trg_todo_steps_status ON todo_steps;
CREATE TRIGGER trg_todo_steps_status
AFTER UPDATE OF status ON todo_steps
FOR EACH ROW EXECUTE FUNCTION propagate_workflow_status(NEW.id);

-- 6. Agent load maintenance
CREATE OR REPLACE FUNCTION refresh_agent_load() RETURNS VOID AS $$
BEGIN
  INSERT INTO todo_agent_load (assigned_worker, in_progress_tasks, pending_tasks, done_tasks, updated_at)
  SELECT
    assigned_worker,
    COUNT(*) FILTER (WHERE status='in_progress'),
    COUNT(*) FILTER (WHERE status='pending'),
    COUNT(*) FILTER (WHERE status='done'),
    now()
  FROM todo_tasks
  WHERE assigned_worker IS NOT NULL
  GROUP BY assigned_worker
  ON CONFLICT (assigned_worker) DO UPDATE SET
    in_progress_tasks = EXCLUDED.in_progress_tasks,
    pending_tasks = EXCLUDED.pending_tasks,
    done_tasks = EXCLUDED.done_tasks,
    updated_at = now();
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_todo_tasks_agent_load ON todo_tasks;
CREATE TRIGGER trg_todo_tasks_agent_load
AFTER INSERT OR UPDATE OF status, assigned_worker ON todo_tasks
FOR EACH ROW EXECUTE FUNCTION refresh_agent_load();

-- Optional: backfill
SELECT refresh_agent_load();
