CREATE TABLE ac_continuity_versions(version integer PRIMARY KEY, sha256 text NOT NULL);
CREATE TABLE ac_recovery_budgets (
    job_id uuid PRIMARY KEY REFERENCES ac_jobs(job_id),
    model_limit integer NOT NULL CHECK(model_limit >= 0),
    tool_limit integer NOT NULL CHECK(tool_limit >= 0),
    model_calls integer NOT NULL DEFAULT 0 CHECK(model_calls BETWEEN 0 AND model_limit),
    tool_calls integer NOT NULL DEFAULT 0 CHECK(tool_calls BETWEEN 0 AND tool_limit),
    deadline timestamptz NOT NULL,
    stop_reason text CHECK(stop_reason IN ('aborted','model_budget_exhausted','tool_budget_exhausted')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ac_budget_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ac_jobs(job_id),
    call_id uuid NOT NULL UNIQUE,
    kind text NOT NULL CHECK(kind IN ('model','tool','aborted','model_budget_exhausted','tool_budget_exhausted')),
    generation bigint NOT NULL,
    attempt_id uuid,
    detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION ac_bootstrap_budget() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest ? 'recovery' THEN
        INSERT INTO ac_recovery_budgets(job_id,model_limit,tool_limit,deadline)
        VALUES(NEW.job_id,(NEW.manifest->'recovery'->>'model_limit')::integer,
          (NEW.manifest->'recovery'->>'tool_limit')::integer,
          clock_timestamp()+make_interval(secs => (NEW.manifest->'recovery'->>'wall_seconds')::integer));
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ac_budget_bootstrap AFTER INSERT ON ac_jobs FOR EACH ROW EXECUTE FUNCTION ac_bootstrap_budget();
CREATE FUNCTION ac_budget_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Durable budgets cannot be deleted' USING ERRCODE='23514'; END IF;
    IF (NEW.job_id,NEW.model_limit,NEW.tool_limit,NEW.deadline,NEW.created_at)
       IS DISTINCT FROM (OLD.job_id,OLD.model_limit,OLD.tool_limit,OLD.deadline,OLD.created_at)
       OR NEW.model_calls<OLD.model_calls OR NEW.tool_calls<OLD.tool_calls
       OR (OLD.stop_reason IS NOT NULL AND NEW.stop_reason IS DISTINCT FROM OLD.stop_reason) THEN
        RAISE EXCEPTION 'Budget identity, limits, deadline and consumed calls cannot reset' USING ERRCODE='23514';
    END IF;
    IF (current_setting('agentcheck.abort',true)='yes' AND NEW.stop_reason='aborted'
            AND NEW.model_calls=OLD.model_calls AND NEW.tool_calls=OLD.tool_calls) IS NOT TRUE THEN
        PERFORM ac_require_lease(NEW.job_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ac_budget_immutable BEFORE UPDATE OR DELETE ON ac_recovery_budgets
    FOR EACH ROW EXECUTE FUNCTION ac_budget_immutable();
CREATE FUNCTION ac_budget_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Budget events are append only' USING ERRCODE='23514'; END $$;
CREATE TRIGGER ac_budget_append_only BEFORE UPDATE OR DELETE ON ac_budget_events
    FOR EACH ROW EXECUTE FUNCTION ac_budget_append_only();
CREATE FUNCTION ac_budget_event_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (current_setting('agentcheck.abort',true)='yes' AND NEW.kind='aborted') IS NOT TRUE THEN
        PERFORM ac_require_lease(NEW.job_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ac_budget_event_fence BEFORE INSERT ON ac_budget_events
    FOR EACH ROW EXECUTE FUNCTION ac_budget_event_fence();
-- Fence saved model/tool results too. Cancellation, deadline and takeover are
-- checked in the database, including the official saver's background thread.
CREATE FUNCTION ac_require_active_budget(target uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE budget ac_recovery_budgets%ROWTYPE;
BEGIN
    PERFORM ac_require_lease(target);
    SELECT * INTO budget FROM ac_recovery_budgets WHERE job_id=target FOR UPDATE;
    IF FOUND AND (budget.stop_reason IS NOT NULL OR budget.deadline<=clock_timestamp()) THEN
        RAISE EXCEPTION 'Recovery stopped or absolute deadline elapsed' USING ERRCODE='23514';
    END IF;
END $$;
CREATE FUNCTION ac_budget_content_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    IF TG_TABLE_NAME IN ('ac_operations','ac_operation_events') THEN
        IF TG_OP='DELETE' THEN target=OLD.job_id; ELSE target=NEW.job_id; END IF;
    ELSE
        IF TG_OP='DELETE' THEN
            SELECT job_id INTO target FROM ac_jobs WHERE thread_id::text=OLD.thread_id;
        ELSE
            SELECT job_id INTO target FROM ac_jobs WHERE thread_id::text=NEW.thread_id;
        END IF;
    END IF;
    IF target IS NOT NULL THEN PERFORM ac_require_active_budget(target); END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER ac_operations_budget BEFORE INSERT OR UPDATE OR DELETE ON ac_operations
    FOR EACH ROW EXECUTE FUNCTION ac_budget_content_write();
CREATE TRIGGER ac_operation_events_budget BEFORE INSERT OR UPDATE OR DELETE ON ac_operation_events
    FOR EACH ROW EXECUTE FUNCTION ac_budget_content_write();
CREATE TRIGGER checkpoints_budget BEFORE INSERT OR UPDATE OR DELETE ON checkpoints
    FOR EACH ROW EXECUTE FUNCTION ac_budget_content_write();
CREATE TRIGGER checkpoint_blobs_budget BEFORE INSERT OR UPDATE OR DELETE ON checkpoint_blobs
    FOR EACH ROW EXECUTE FUNCTION ac_budget_content_write();
CREATE TRIGGER checkpoint_writes_budget BEFORE INSERT OR UPDATE OR DELETE ON checkpoint_writes
    FOR EACH ROW EXECUTE FUNCTION ac_budget_content_write();
