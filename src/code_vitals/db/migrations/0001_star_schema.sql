-- The star schema. Every table here can be rebuilt from git by re-running the
-- collector over each repo's history, except the rows of measures flagged
-- non-recomputable, which start from their release date.

-- Dimensions -------------------------------------------------------------

CREATE TABLE repo (
    id     smallserial PRIMARY KEY,
    name   text NOT NULL UNIQUE,
    trunk  text NOT NULL
);

-- One trunk commit. Squash merges make this one iteration's worth of change.
-- `iteration` is the minor of the repo's version file at that commit; `tags`
-- are the release tags pointing at it.
CREATE TABLE commit (
    id            bigserial PRIMARY KEY,
    repo_id       smallint NOT NULL REFERENCES repo (id),
    sha           text NOT NULL CHECK (sha ~ '^[0-9a-f]{40}$'),
    committed_at  timestamptz NOT NULL,
    author        text NOT NULL,
    subject       text NOT NULL,
    pr_number     integer,
    iteration     integer,
    tags          text[] NOT NULL DEFAULT '{}',
    UNIQUE (repo_id, sha)
);
CREATE INDEX commit_repo_committed_at ON commit (repo_id, committed_at);

CREATE TYPE measure_kind AS ENUM (
    'snapshot',  -- a state at the commit: take the last value per period
    'flow'       -- a change made by the commit: sum per period
);

-- A measure definition's lifecycle. A new version starts `backfilling`; the
-- current_* views switch to it only once it is `complete`, so charts never mix
-- two definitions of one measure.
CREATE TYPE version_status AS ENUM ('backfilling', 'complete', 'retired');

CREATE TABLE measure (
    id            serial PRIMARY KEY,
    name          text NOT NULL,
    version       integer NOT NULL CHECK (version > 0),
    unit          text NOT NULL,
    kind          measure_kind NOT NULL,
    definition    text NOT NULL,
    recomputable  boolean NOT NULL,
    status        version_status NOT NULL DEFAULT 'backfilling',
    released_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

-- A repo's path rules that split it into areas, versioned like a measure.
CREATE TABLE area_ruleset (
    id           serial PRIMARY KEY,
    repo_id      smallint NOT NULL REFERENCES repo (id),
    version      integer NOT NULL CHECK (version > 0),
    rules        jsonb NOT NULL,
    status       version_status NOT NULL DEFAULT 'backfilling',
    released_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repo_id, version)
);

CREATE TABLE area (
    id          serial PRIMARY KEY,
    ruleset_id  integer NOT NULL REFERENCES area_ruleset (id),
    name        text NOT NULL,
    UNIQUE (ruleset_id, name)
);

-- Facts ------------------------------------------------------------------

-- One value of one measure version at one commit, optionally split by area and
-- by a facet (language, kind, suite …; '' when unsplit). Unique over all but
-- the value, so a re-run of the collector writes nothing twice.
CREATE TABLE measurement (
    id          bigserial PRIMARY KEY,
    commit_id   bigint NOT NULL REFERENCES commit (id),
    measure_id  integer NOT NULL REFERENCES measure (id),
    area_id     integer REFERENCES area (id),
    facet       text NOT NULL DEFAULT '',
    value       double precision NOT NULL,
    CONSTRAINT measurement_key UNIQUE NULLS NOT DISTINCT (commit_id, measure_id, area_id, facet)
);
CREATE INDEX measurement_measure ON measurement (measure_id);

-- Per-function hotspot rows, versioned through a measure (the lizard version
-- is part of that measure's version).
CREATE TABLE function_complexity (
    id          bigserial PRIMARY KEY,
    commit_id   bigint NOT NULL REFERENCES commit (id),
    measure_id  integer NOT NULL REFERENCES measure (id),
    file        text NOT NULL,
    function    text NOT NULL,
    start_line  integer NOT NULL,
    ccn         integer NOT NULL,
    nloc        integer NOT NULL,
    params      integer NOT NULL,
    UNIQUE (commit_id, measure_id, file, function, start_line)
);
CREATE INDEX function_complexity_measure ON function_complexity (measure_id);

CREATE TYPE run_trigger AS ENUM ('push', 'backfill', 'manual');
CREATE TYPE run_outcome AS ENUM ('running', 'success', 'failure');

-- One collector run, so every row can be traced to the build that wrote it.
CREATE TABLE run (
    id                  bigserial PRIMARY KEY,
    repo_id             smallint NOT NULL REFERENCES repo (id),
    trigger             run_trigger NOT NULL,
    collector_version   text NOT NULL,
    collector_revision  text NOT NULL,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    outcome             run_outcome NOT NULL DEFAULT 'running',
    commits_measured    integer NOT NULL DEFAULT 0,
    trace_id            text
);
CREATE INDEX run_repo_started_at ON run (repo_id, started_at);

-- Views: the only surface Superset reads ---------------------------------

-- Per measure name, the newest version whose backfill is complete.
CREATE VIEW current_measure AS
SELECT DISTINCT ON (name) *
FROM measure
WHERE status = 'complete'
ORDER BY name, version DESC;

-- Per repo, the newest area ruleset whose backfill is complete.
CREATE VIEW current_area_ruleset AS
SELECT DISTINCT ON (repo_id) *
FROM area_ruleset
WHERE status = 'complete'
ORDER BY repo_id, version DESC;

-- Every value of a current measure version, with its commit, date grains and
-- area. Area-split rows count only when their area belongs to the repo's
-- current ruleset; unsplit rows (no area) always count.
CREATE VIEW current_measurement AS
SELECT
    r.name                                          AS repo,
    c.sha,
    c.committed_at,
    (date_trunc('day', c.committed_at))::date       AS day,
    (date_trunc('week', c.committed_at))::date      AS week,
    (date_trunc('month', c.committed_at))::date     AS month,
    c.iteration,
    c.pr_number,
    c.subject,
    c.tags,
    m.name                                          AS measure,
    m.version                                       AS measure_version,
    m.unit,
    m.kind,
    a.name                                          AS area,
    x.facet,
    x.value
FROM measurement x
JOIN current_measure m ON m.id = x.measure_id
JOIN commit c ON c.id = x.commit_id
JOIN repo r ON r.id = c.repo_id
LEFT JOIN area a ON a.id = x.area_id
LEFT JOIN current_area_ruleset rs ON rs.id = a.ruleset_id AND rs.repo_id = c.repo_id
WHERE x.area_id IS NULL OR rs.id IS NOT NULL;

CREATE VIEW current_function_complexity AS
SELECT
    r.name          AS repo,
    c.sha,
    c.committed_at,
    c.iteration,
    m.name          AS measure,
    m.version       AS measure_version,
    f.file,
    f.function,
    f.start_line,
    f.ccn,
    f.nloc,
    f.params
FROM function_complexity f
JOIN current_measure m ON m.id = f.measure_id
JOIN commit c ON c.id = f.commit_id
JOIN repo r ON r.id = c.repo_id;

-- Per repo: the newest commit with any recorded value, its age, and the last
-- collector run. The health check of the whole pipeline: it also shows a
-- collector that never ran.
CREATE VIEW freshness AS
SELECT
    r.name                    AS repo,
    lc.sha                    AS last_recorded_sha,
    lc.committed_at           AS last_recorded_committed_at,
    now() - lc.committed_at   AS last_recorded_age,
    lr.started_at             AS last_run_started_at,
    lr.finished_at            AS last_run_finished_at,
    lr.outcome                AS last_run_outcome
FROM repo r
LEFT JOIN LATERAL (
    SELECT c.sha, c.committed_at
    FROM commit c
    WHERE c.repo_id = r.id
      AND EXISTS (SELECT 1 FROM measurement x WHERE x.commit_id = c.id)
    ORDER BY c.committed_at DESC
    LIMIT 1
) lc ON true
LEFT JOIN LATERAL (
    SELECT started_at, finished_at, outcome
    FROM run
    WHERE run.repo_id = r.id
    ORDER BY started_at DESC
    LIMIT 1
) lr ON true;
