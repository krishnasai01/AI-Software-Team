-- Phase 1 to 3 data model. GitHub is what humans see; these tables are what the system trusts.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE products (
    id                 BIGSERIAL PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    repo               TEXT NOT NULL UNIQUE,          -- "owner/name"
    project_board_id   TEXT,                          -- GitHub Projects v2 node id (optional mirror)
    sandbox_image      TEXT NOT NULL,
    default_branch     TEXT NOT NULL DEFAULT 'main',
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agents (
    role               TEXT PRIMARY KEY,              -- engineer, reviewer, ...
    model              TEXT NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    daily_cap_usd      NUMERIC(10, 2) NOT NULL,
    github_app_id      BIGINT
);

CREATE TABLE claims (
    id                 BIGSERIAL PRIMARY KEY,
    ticket             TEXT NOT NULL,                 -- "owner/name#123"
    agent              TEXT NOT NULL REFERENCES agents(role),
    workflow_id        TEXT NOT NULL,
    -- active: an agent is working and must heartbeat; waiting: parked on review or a human,
    -- owned by a live workflow, so no lease; released / expired: finished.
    status             TEXT NOT NULL CHECK (status IN ('active', 'waiting', 'released', 'expired')),
    lease_expires_at   TIMESTAMPTZ,
    last_heartbeat     TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at        TIMESTAMPTZ
);
-- At most one live claim per ticket.
CREATE UNIQUE INDEX claims_one_live_per_ticket ON claims (ticket) WHERE status IN ('active', 'waiting');

CREATE TABLE runs (
    id                 TEXT PRIMARY KEY,              -- also used as the Langfuse trace id
    agent              TEXT NOT NULL REFERENCES agents(role),
    ticket             TEXT,
    workflow_id        TEXT,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at           TIMESTAMPTZ,
    outcome            TEXT,                          -- completed, blocked, error, timeout, budget
    turns              INTEGER,
    tokens_in          BIGINT,
    tokens_out         BIGINT,
    cost_usd           NUMERIC(10, 4),
    trace_url          TEXT
);
CREATE INDEX runs_agent_started ON runs (agent, started_at);

CREATE TABLE spend_daily (
    agent              TEXT NOT NULL REFERENCES agents(role),
    day                DATE NOT NULL,
    spent_usd          NUMERIC(10, 4) NOT NULL DEFAULT 0,
    cap_usd            NUMERIC(10, 2) NOT NULL,
    PRIMARY KEY (agent, day)
);

CREATE TABLE approvals (
    id                 BIGSERIAL PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('merge', 'deploy', 'publish', 'budget_raise')),
    target             TEXT NOT NULL,                 -- PR url, deploy id, agent role, ...
    workflow_id        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by         TEXT,
    decided_at         TIMESTAMPTZ,
    chat_message_link  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memories (
    id                 BIGSERIAL PRIMARY KEY,
    product            TEXT NOT NULL REFERENCES products(name),
    kind               TEXT NOT NULL,                 -- lesson, pitfall, decision
    text               TEXT NOT NULL,
    embedding          vector(1024),
    source_run         TEXT REFERENCES runs(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events_inbox (
    delivery_id        TEXT PRIMARY KEY,              -- X-GitHub-Delivery; de-duplicates redeliveries
    event              TEXT NOT NULL,
    payload            JSONB NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    error              TEXT
);
CREATE INDEX events_inbox_unprocessed ON events_inbox (received_at) WHERE processed_at IS NULL;

CREATE TABLE incidents (
    id                 BIGSERIAL PRIMARY KEY,
    kind               TEXT NOT NULL,
    agent              TEXT,
    ticket             TEXT,
    action_taken       TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

-- Phase 1 roles. Caps match the plan's roster; change them here or in the agents table, never from an agent.
INSERT INTO agents (role, model, daily_cap_usd) VALUES
    ('engineer', 'claude-opus-5-5', 50),
    ('reviewer', 'claude-sonnet-5-5', 15);
