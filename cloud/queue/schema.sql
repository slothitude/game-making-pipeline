-- GMP job queue — the one table the whole Pipeline serializes through.
-- The 956MB retromonkey box survives by SERIALIZING work: one job advances
-- at a time, the rest wait here with status 'queued'.
--
-- Install (on retromonkey):
--   PGPASSWORD=slothitude2026 psql -h localhost -U pipeline -d gmp -f schema.sql
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS jobs (
    id          serial PRIMARY KEY,
    type        text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'queued',
    priority    int DEFAULT 5,
    result      jsonb,
    error       text,
    attempts    int NOT NULL DEFAULT 0,
    created_at  timestamptz DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);

-- The claim query rides this index: WHERE status='queued'
-- ORDER BY priority ASC, created_at ASC.
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (status, priority, created_at);
