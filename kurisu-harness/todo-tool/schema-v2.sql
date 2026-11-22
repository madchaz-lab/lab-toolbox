--
-- PostgreSQL database dump
--


-- Dumped from database version 17.11 (Debian 17.11-1.pgdg13+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: kurisu_todo; Type: SCHEMA; Schema: -; Owner: kurisu
--

CREATE SCHEMA kurisu_todo;


ALTER SCHEMA kurisu_todo OWNER TO kurisu;

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: prevent_update_delete(); Type: FUNCTION; Schema: kurisu_todo; Owner: kurisu
--

CREATE FUNCTION kurisu_todo.prevent_update_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'todo_comments is append-only; UPDATE/DELETE prohibited';
END;
$$;


ALTER FUNCTION kurisu_todo.prevent_update_delete() OWNER TO kurisu;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: kurisu_todo; Owner: kurisu
--

CREATE FUNCTION kurisu_todo.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION kurisu_todo.set_updated_at() OWNER TO kurisu;

--
-- Name: validate_metadata_agent(); Type: FUNCTION; Schema: kurisu_todo; Owner: kurisu
--

CREATE FUNCTION kurisu_todo.validate_metadata_agent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;


ALTER FUNCTION kurisu_todo.validate_metadata_agent() OWNER TO kurisu;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: todo_work_orders; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_work_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    step_id uuid NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    assigned_worker text,
    model_hint text,
    resource text,
    artifact_path text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    assigned_subagent text,
    assigned_at timestamp with time zone,
    completed_at timestamp with time zone,
    subagent_session_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT todo_tasks_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text, 'done'::text, 'cancelled'::text])))
);


ALTER TABLE public.todo_work_orders OWNER TO kurisu;

--
-- Name: todo_agent_load; Type: VIEW; Schema: public; Owner: kurisu
--

CREATE VIEW public.todo_agent_load AS
 SELECT assigned_worker,
    count(*) FILTER (WHERE (status = 'open'::text)) AS open_tasks,
    count(*) FILTER (WHERE (status = 'in_progress'::text)) AS in_progress_tasks,
    count(*) FILTER (WHERE (status = 'done'::text)) AS done_tasks,
    now() AS updated_at
   FROM public.todo_work_orders
  WHERE (assigned_worker IS NOT NULL)
  GROUP BY assigned_worker;


ALTER VIEW public.todo_agent_load OWNER TO kurisu;

--
-- Name: todo_comments; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_comments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    author text NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT todo_comments_entity_type_check CHECK ((entity_type = ANY (ARRAY['project'::text, 'task'::text, 'step'::text, 'work_order'::text, 'template'::text])))
);


ALTER TABLE public.todo_comments OWNER TO kurisu;

--
-- Name: todo_migrations; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_migrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    description text NOT NULL
);


ALTER TABLE public.todo_migrations OWNER TO kurisu;

--
-- Name: todo_projects; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    description text,
    bank_id text,
    status text DEFAULT 'open'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT todo_projects_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text, 'done'::text, 'cancelled'::text])))
);


ALTER TABLE public.todo_projects OWNER TO kurisu;

--
-- Name: todo_steps; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    name text NOT NULL,
    order_idx integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    depends_on uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT todo_steps_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text, 'done'::text, 'cancelled'::text])))
);


ALTER TABLE public.todo_steps OWNER TO kurisu;

--
-- Name: todo_tasks; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    bank_id text,
    status text DEFAULT 'open'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    depends_on uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    template_id uuid,
    template_version integer,
    CONSTRAINT todo_workflows_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text, 'done'::text, 'cancelled'::text])))
);


ALTER TABLE public.todo_tasks OWNER TO kurisu;

--
-- Name: todo_transition_events; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_transition_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    from_status text,
    to_status text NOT NULL,
    actor_class text NOT NULL,
    actor_id text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT todo_transition_events_actor_class_check CHECK ((actor_class = ANY (ARRAY['worker'::text, 'step_owner'::text, 'orchestrator'::text, 'validator'::text]))),
    CONSTRAINT todo_transition_events_entity_type_check CHECK ((entity_type = ANY (ARRAY['project'::text, 'task'::text, 'step'::text, 'work_order'::text]))),
    CONSTRAINT todo_transition_events_to_status_check CHECK ((to_status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text, 'done'::text, 'cancelled'::text])))
);


ALTER TABLE public.todo_transition_events OWNER TO kurisu;

--
-- Name: todo_workflow_template_versions; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_workflow_template_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    version integer NOT NULL,
    step_dag jsonb NOT NULL,
    created_by text DEFAULT 'orchestrator'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.todo_workflow_template_versions OWNER TO kurisu;

--
-- Name: todo_workflow_templates; Type: TABLE; Schema: public; Owner: kurisu
--

CREATE TABLE public.todo_workflow_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.todo_workflow_templates OWNER TO kurisu;

--
-- Name: todo_comments todo_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_comments
    ADD CONSTRAINT todo_comments_pkey PRIMARY KEY (id);


--
-- Name: todo_migrations todo_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_migrations
    ADD CONSTRAINT todo_migrations_pkey PRIMARY KEY (id);


--
-- Name: todo_projects todo_projects_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_projects
    ADD CONSTRAINT todo_projects_pkey PRIMARY KEY (id);


--
-- Name: todo_steps todo_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_steps
    ADD CONSTRAINT todo_steps_pkey PRIMARY KEY (id);


--
-- Name: todo_work_orders todo_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_work_orders
    ADD CONSTRAINT todo_tasks_pkey PRIMARY KEY (id);


--
-- Name: todo_transition_events todo_transition_events_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_transition_events
    ADD CONSTRAINT todo_transition_events_pkey PRIMARY KEY (id);


--
-- Name: todo_workflow_template_versions todo_workflow_template_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_workflow_template_versions
    ADD CONSTRAINT todo_workflow_template_versions_pkey PRIMARY KEY (id);


--
-- Name: todo_workflow_template_versions todo_workflow_template_versions_template_id_version_key; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_workflow_template_versions
    ADD CONSTRAINT todo_workflow_template_versions_template_id_version_key UNIQUE (template_id, version);


--
-- Name: todo_workflow_templates todo_workflow_templates_name_key; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_workflow_templates
    ADD CONSTRAINT todo_workflow_templates_name_key UNIQUE (name);


--
-- Name: todo_workflow_templates todo_workflow_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_workflow_templates
    ADD CONSTRAINT todo_workflow_templates_pkey PRIMARY KEY (id);


--
-- Name: todo_tasks todo_workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_tasks
    ADD CONSTRAINT todo_workflows_pkey PRIMARY KEY (id);


--
-- Name: idx_todo_comments_entity; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_comments_entity ON public.todo_comments USING btree (entity_type, entity_id);


--
-- Name: idx_todo_steps_workflow_id; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_steps_workflow_id ON public.todo_steps USING btree (task_id);


--
-- Name: idx_todo_tasks_assigned_worker; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_tasks_assigned_worker ON public.todo_work_orders USING btree (assigned_worker);


--
-- Name: idx_todo_tasks_step_id; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_tasks_step_id ON public.todo_work_orders USING btree (step_id);


--
-- Name: idx_todo_tasks_subagent_session_id; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_tasks_subagent_session_id ON public.todo_work_orders USING btree (subagent_session_id) WHERE (subagent_session_id IS NOT NULL);


--
-- Name: idx_todo_tasks_subagent_session_unique; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE UNIQUE INDEX idx_todo_tasks_subagent_session_unique ON public.todo_work_orders USING btree (subagent_session_id) WHERE ((subagent_session_id IS NOT NULL) AND (status = ANY (ARRAY['open'::text, 'in_progress'::text, 'done_proposed'::text])));


--
-- Name: idx_todo_transition_events_entity; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_transition_events_entity ON public.todo_transition_events USING btree (entity_type, entity_id);


--
-- Name: idx_todo_workflows_project_id; Type: INDEX; Schema: public; Owner: kurisu
--

CREATE INDEX idx_todo_workflows_project_id ON public.todo_tasks USING btree (project_id);


--
-- Name: todo_comments trg_comments_append_only; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_comments_append_only BEFORE DELETE OR UPDATE ON public.todo_comments FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();


--
-- Name: todo_projects trg_set_updated_at_projects; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_set_updated_at_projects BEFORE UPDATE ON public.todo_projects FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();


--
-- Name: todo_steps trg_set_updated_at_steps; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_set_updated_at_steps BEFORE UPDATE ON public.todo_steps FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();


--
-- Name: todo_work_orders trg_set_updated_at_tasks; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_set_updated_at_tasks BEFORE UPDATE ON public.todo_work_orders FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();


--
-- Name: todo_tasks trg_set_updated_at_workflows; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_set_updated_at_workflows BEFORE UPDATE ON public.todo_tasks FOR EACH ROW EXECUTE FUNCTION kurisu_todo.set_updated_at();


--
-- Name: todo_transition_events trg_transition_events_append_only; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_transition_events_append_only BEFORE DELETE OR UPDATE ON public.todo_transition_events FOR EACH ROW EXECUTE FUNCTION kurisu_todo.prevent_update_delete();


--
-- Name: todo_projects trg_validate_metadata_agent_projects; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_validate_metadata_agent_projects BEFORE INSERT OR UPDATE OF metadata ON public.todo_projects FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();


--
-- Name: todo_steps trg_validate_metadata_agent_steps; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_validate_metadata_agent_steps BEFORE INSERT OR UPDATE OF metadata ON public.todo_steps FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();


--
-- Name: todo_work_orders trg_validate_metadata_agent_tasks; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_validate_metadata_agent_tasks BEFORE INSERT OR UPDATE OF metadata ON public.todo_work_orders FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();


--
-- Name: todo_tasks trg_validate_metadata_agent_workflows; Type: TRIGGER; Schema: public; Owner: kurisu
--

CREATE TRIGGER trg_validate_metadata_agent_workflows BEFORE INSERT OR UPDATE OF metadata ON public.todo_tasks FOR EACH ROW EXECUTE FUNCTION kurisu_todo.validate_metadata_agent();


--
-- Name: todo_steps todo_steps_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_steps
    ADD CONSTRAINT todo_steps_workflow_id_fkey FOREIGN KEY (task_id) REFERENCES public.todo_tasks(id) ON DELETE RESTRICT;


--
-- Name: todo_work_orders todo_tasks_step_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_work_orders
    ADD CONSTRAINT todo_tasks_step_id_fkey FOREIGN KEY (step_id) REFERENCES public.todo_steps(id) ON DELETE RESTRICT;


--
-- Name: todo_tasks todo_tasks_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_tasks
    ADD CONSTRAINT todo_tasks_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.todo_workflow_templates(id) ON DELETE RESTRICT;


--
-- Name: todo_workflow_template_versions todo_workflow_template_versions_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_workflow_template_versions
    ADD CONSTRAINT todo_workflow_template_versions_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.todo_workflow_templates(id) ON DELETE RESTRICT;


--
-- Name: todo_tasks todo_workflows_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kurisu
--

ALTER TABLE ONLY public.todo_tasks
    ADD CONSTRAINT todo_workflows_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.todo_projects(id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--


