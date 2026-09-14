CREATE TABLE ac_scheduler_versions(version integer PRIMARY KEY, sha256 text NOT NULL);
CREATE TABLE ac_leases (
    job_id uuid PRIMARY KEY REFERENCES ac_jobs(job_id),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    owner uuid,
    lease_until timestamptz,
    CHECK ((owner IS NULL) = (lease_until IS NULL))
);
CREATE TABLE ac_lease_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ac_jobs(job_id),
    generation bigint NOT NULL,
    owner uuid,
    kind text NOT NULL,
    detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Serialize each protected write against takeover using the same lease row.
-- This guards application paths, including an accidentally used legacy saver.
-- It is not an authorization boundary against an administrator with arbitrary SQL.
CREATE FUNCTION ac_require_lease(target uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE current_lease ac_leases%ROWTYPE;
BEGIN
    SELECT * INTO current_lease FROM ac_leases WHERE job_id=target FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF current_setting('agentcheck.expire', true) = 'yes'
       AND current_lease.owner IS NOT NULL
       AND current_lease.lease_until <= clock_timestamp() THEN RETURN; END IF;
    IF current_lease.owner IS NULL
       OR current_lease.lease_until <= clock_timestamp()
       OR current_lease.owner::text IS DISTINCT FROM current_setting('agentcheck.owner', true)
       OR current_lease.generation::text IS DISTINCT FROM current_setting('agentcheck.generation', true) THEN
        RAISE EXCEPTION 'Execution lease is absent, expired or superseded' USING ERRCODE='23514';
    END IF;
END $$;
CREATE FUNCTION ac_fence_job_write() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN PERFORM ac_require_lease(OLD.job_id); END IF;
    IF TG_OP <> 'DELETE' THEN PERFORM ac_require_lease(NEW.job_id); RETURN NEW; END IF;
    RETURN OLD;
END $$;
CREATE FUNCTION ac_fence_checkpoint_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        SELECT job_id INTO target FROM ac_jobs WHERE thread_id::text=OLD.thread_id;
        IF target IS NOT NULL THEN PERFORM ac_require_lease(target); END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        SELECT job_id INTO target FROM ac_jobs WHERE thread_id::text=NEW.thread_id;
        IF target IS NOT NULL THEN PERFORM ac_require_lease(target); END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER ac_jobs_fenced BEFORE INSERT OR UPDATE OR DELETE ON ac_jobs
    FOR EACH ROW EXECUTE FUNCTION ac_fence_job_write();
CREATE TRIGGER ac_attempts_fenced BEFORE INSERT OR UPDATE OR DELETE ON ac_attempts
    FOR EACH ROW EXECUTE FUNCTION ac_fence_job_write();
CREATE TRIGGER ac_job_events_fenced BEFORE INSERT OR UPDATE OR DELETE ON ac_job_events
    FOR EACH ROW EXECUTE FUNCTION ac_fence_job_write();
CREATE TRIGGER checkpoints_fenced BEFORE INSERT OR UPDATE OR DELETE ON checkpoints
    FOR EACH ROW EXECUTE FUNCTION ac_fence_checkpoint_write();
CREATE TRIGGER checkpoint_blobs_fenced BEFORE INSERT OR UPDATE OR DELETE ON checkpoint_blobs
    FOR EACH ROW EXECUTE FUNCTION ac_fence_checkpoint_write();
CREATE TRIGGER checkpoint_writes_fenced BEFORE INSERT OR UPDATE OR DELETE ON checkpoint_writes
    FOR EACH ROW EXECUTE FUNCTION ac_fence_checkpoint_write();
