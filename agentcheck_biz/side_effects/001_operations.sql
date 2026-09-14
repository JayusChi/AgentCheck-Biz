CREATE TABLE ac_operation_versions(version integer PRIMARY KEY, sha256 text NOT NULL);
CREATE TABLE ac_operations (
    job_id uuid PRIMARY KEY REFERENCES ac_jobs(job_id),
    environment_id uuid NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ticket', 'gitea')),
    scope text NOT NULL,
    operation_id text NOT NULL,
    request_sha256 text NOT NULL,
    binding jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','sent_unknown','confirmed','conflict')),
    result jsonb,
    revision integer NOT NULL DEFAULT 0,
    UNIQUE(environment_id,object_kind,scope,operation_id),
    CHECK ((state='confirmed') = (result IS NOT NULL))
);
CREATE TABLE ac_operation_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ac_operations(job_id),
    revision integer NOT NULL,
    state text NOT NULL,
    generation bigint NOT NULL,
    attempt_id uuid NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(job_id,revision)
);
CREATE TRIGGER ac_operations_fenced BEFORE INSERT OR UPDATE OR DELETE ON ac_operations
    FOR EACH ROW EXECUTE FUNCTION ac_fence_job_write();
CREATE TRIGGER ac_operation_events_fenced BEFORE INSERT OR UPDATE OR DELETE ON ac_operation_events
    FOR EACH ROW EXECUTE FUNCTION ac_fence_job_write();
