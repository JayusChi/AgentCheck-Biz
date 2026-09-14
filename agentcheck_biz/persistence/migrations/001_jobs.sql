CREATE TABLE ac_jobs (
    job_id uuid PRIMARY KEY,
    experiment_id uuid NOT NULL UNIQUE,
    environment_id uuid NOT NULL UNIQUE,
    thread_id uuid NOT NULL UNIQUE,
    run_id text NOT NULL UNIQUE,
    operation_id text NOT NULL,
    evidence_dir text NOT NULL UNIQUE,
    request_sha256 text NOT NULL CHECK (length(request_sha256) = 64),
    manifest jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','waiting_verification','finished','error','interrupted')),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    attempt_id uuid,
    checkpoint_id text,
    storage_version integer NOT NULL CHECK (storage_version = 1),
    graph_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ac_attempts (
    attempt_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ac_jobs(job_id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    pid integer NOT NULL CHECK (pid > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id, ordinal)
);
CREATE TABLE ac_job_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ac_jobs(job_id),
    attempt_id uuid REFERENCES ac_attempts(attempt_id),
    previous_state text,
    state text NOT NULL,
    revision integer NOT NULL,
    detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id, revision)
);
