-- v2 (docs/design-v2.md): a level-triggered reconciler and a Postgres job queue replace Temporal,
-- plus the state that autonomy needs (policy decisions, autonomy levels, merges, questions).

DROP TABLE claims;  -- jobs are the leases now

INSERT INTO agents (role, model, daily_cap_usd) VALUES ('techlead', 'claude-opus-5-5', 20);
ALTER TABLE agents ADD COLUMN litellm_team_id TEXT;

CREATE TABLE jobs (
    id                 BIGSERIAL PRIMARY KEY,
    kind               TEXT NOT NULL REFERENCES agents(role),
    mode               TEXT NOT NULL,               -- implement, rework, fix-ci, update, review, clarify, ...
    product            TEXT NOT NULL REFERENCES products(name),
    ticket             INTEGER,                     -- issue number; NULL for an outsider PR review
    pr                 INTEGER,
    head_sha           TEXT,
    status             TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt            INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL DEFAULT 3,
    not_before         TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at   TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    worker             TEXT,
    input              JSONB NOT NULL DEFAULT '{}',
    result             JSONB,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ
);
-- One live job per ticket (or per outsider PR): the agents never trip over each other.
CREATE UNIQUE INDEX jobs_one_live_per_ticket ON jobs (product, ticket)
    WHERE status IN ('queued', 'running') AND ticket IS NOT NULL;
CREATE UNIQUE INDEX jobs_one_live_per_pr ON jobs (product, pr)
    WHERE status IN ('queued', 'running') AND ticket IS NULL;
CREATE INDEX jobs_claimable ON jobs (not_before) WHERE status = 'queued';
CREATE INDEX jobs_history ON jobs (product, ticket, id);

ALTER TABLE runs DROP COLUMN workflow_id;
ALTER TABLE runs ADD COLUMN job_id BIGINT REFERENCES jobs(id);

-- What the reconciler remembers about a ticket that GitHub does not tell it cheaply.
CREATE TABLE tickets (
    product            TEXT NOT NULL REFERENCES products(name),
    number             INTEGER NOT NULL,
    labeled_by         TEXT,                        -- who applied agent-ready (must be trusted)
    external_origin    BOOLEAN NOT NULL DEFAULT FALSE,
    parent             INTEGER,                     -- set for tickets a tech lead split out
    head_sha           TEXT,
    head_seen_at       TIMESTAMPTZ,                 -- CI grace period starts here
    ci_rerun_sha       TEXT,                        -- head we already re-ran failed CI for
    update_from_sha    TEXT,                        -- head we asked GitHub to bring up to date
    status_note        TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product, number)
);

CREATE TABLE autonomy (
    product            TEXT PRIMARY KEY REFERENCES products(name),
    level              INTEGER NOT NULL DEFAULT 0 CHECK (level BETWEEN 0 AND 3),
    auto_merge_paused  BOOLEAN NOT NULL DEFAULT FALSE,
    changed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by         TEXT NOT NULL DEFAULT 'default',
    reason             TEXT
);

CREATE TABLE policy_decisions (
    product            TEXT NOT NULL REFERENCES products(name),
    pr                 INTEGER NOT NULL,
    head_sha           TEXT NOT NULL,
    tier               TEXT NOT NULL CHECK (tier IN ('auto', 'human')),
    next_level_tier    TEXT NOT NULL CHECK (next_level_tier IN ('auto', 'human')),  -- shadow record
    level              INTEGER NOT NULL,
    reasons            JSONB NOT NULL DEFAULT '[]',
    satisfied          BOOLEAN NOT NULL DEFAULT FALSE,  -- check posted green (auto, or you approved)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product, pr, head_sha)
);

-- Track record: every merge of an agent PR, watched after merging; drives promotion and demotion.
CREATE TABLE merges (
    product            TEXT NOT NULL REFERENCES products(name),
    pr                 INTEGER NOT NULL,
    ticket             INTEGER,
    merge_sha          TEXT,
    auto               BOOLEAN NOT NULL,
    level              INTEGER NOT NULL,
    next_level_tier    TEXT,
    merged_at          TIMESTAMPTZ NOT NULL,
    watch_until        TIMESTAMPTZ NOT NULL,
    outcome            TEXT CHECK (outcome IN ('ok', 'broke_main', 'reverted')),
    revert_pr          INTEGER,
    PRIMARY KEY (product, pr)
);

CREATE TABLE questions (
    id                 BIGSERIAL PRIMARY KEY,
    product            TEXT NOT NULL REFERENCES products(name),
    ticket             INTEGER NOT NULL,
    question           TEXT NOT NULL,
    options            JSONB NOT NULL DEFAULT '[]',
    default_answer     TEXT,
    reversible         BOOLEAN NOT NULL DEFAULT FALSE,  -- only reversible questions may default
    deadline           TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'defaulted', 'cancelled')),
    answer             TEXT,
    answered_by        TEXT,
    answered_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX questions_one_open_per_ticket ON questions (product, ticket) WHERE status = 'open';

-- Kill switch, ceiling override, digest bookkeeping.
CREATE TABLE controls (
    key                TEXT PRIMARY KEY,
    value              JSONB NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by         TEXT
);

ALTER TABLE memories ADD COLUMN provenance TEXT NOT NULL DEFAULT 'agent' CHECK (provenance IN ('agent', 'human'));
ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'));
ALTER TABLE memories ADD COLUMN ticket INTEGER;

ALTER TABLE approvals DROP CONSTRAINT approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check
    CHECK (kind IN ('merge', 'deploy', 'publish', 'budget_raise', 'promotion', 'policy'));
ALTER TABLE approvals DROP COLUMN workflow_id;
ALTER TABLE approvals ADD COLUMN product TEXT REFERENCES products(name);
ALTER TABLE approvals ADD COLUMN payload JSONB NOT NULL DEFAULT '{}';
