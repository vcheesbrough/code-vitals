-- A repo a deployment could not re-measure — its workflow file has no
-- code-vitals plugin step (any more, or not yet), or it has no URL — is
-- *skipped* (card #572). `skipped_at` is when a deployment first found it so;
-- NULL means active. The collector maintains it: a deployment's backfill sets
-- it, and the repo's next run that catches it up on every registered version
-- clears it.
--
-- A skipped repo does not count toward completing a measure version (it never
-- backfills, so it would keep every new version `backfilling` forever), and its
-- recorded history stays visible, frozen: the views below show it at the
-- newest complete version of each measure *it* finished backfilling, while
-- active repos show the newest complete version as before.
ALTER TABLE repo ADD COLUMN skipped_at timestamptz;

-- Per repo and measure name, the version the views read. Active repos: the
-- newest complete version (current_measure). Skipped repos: the newest
-- complete version the repo has a measure_backfill row for, so a version
-- completed without it does not blank its charts, and a repo coming back
-- keeps its frozen version until the run that clears `skipped_at` finishes.
CREATE VIEW repo_current_measure AS
SELECT r.id AS repo_id, m.id, m.name, m.version, m.unit, m.kind, m.recomputable, m.released_at
FROM repo r
CROSS JOIN current_measure m
WHERE r.skipped_at IS NULL
UNION ALL
SELECT s.repo_id, s.id, s.name, s.version, s.unit, s.kind, s.recomputable, s.released_at
FROM (
    SELECT DISTINCT ON (r.id, m.name)
        r.id AS repo_id, m.id, m.name, m.version, m.unit, m.kind, m.recomputable, m.released_at
    FROM repo r
    JOIN measure_backfill b ON b.repo_id = r.id
    JOIN measure m ON m.id = b.measure_id
    WHERE r.skipped_at IS NOT NULL AND m.status = 'complete'
    ORDER BY r.id, m.name, m.version DESC
) s;

-- The same columns as before, in the same order; only the measure join changed.
CREATE OR REPLACE VIEW current_measurement AS
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
    x.value,
    m.recomputable                                  AS measure_recomputable,
    m.released_at                                   AS measure_released_at
FROM measurement x
JOIN commit c ON c.id = x.commit_id
JOIN repo r ON r.id = c.repo_id
JOIN repo_current_measure m ON m.id = x.measure_id AND m.repo_id = c.repo_id
LEFT JOIN area a ON a.id = x.area_id
LEFT JOIN current_area_ruleset rs ON rs.id = a.ruleset_id AND rs.repo_id = c.repo_id
WHERE x.area_id IS NULL OR rs.id IS NOT NULL;

CREATE OR REPLACE VIEW current_function_complexity AS
SELECT
    r.name          AS repo,
    c.sha,
    c.committed_at,
    c.iteration,
    m.name          AS measure,
    m.version       AS measure_version,
    a.name          AS area,
    f.file,
    f.function,
    f.start_line,
    f.ccn,
    f.nloc,
    f.params
FROM function_complexity f
JOIN commit c ON c.id = f.commit_id
JOIN repo r ON r.id = c.repo_id
JOIN repo_current_measure m ON m.id = f.measure_id AND m.repo_id = c.repo_id
LEFT JOIN area a ON a.id = f.area_id
LEFT JOIN current_area_ruleset rs ON rs.id = a.ruleset_id AND rs.repo_id = c.repo_id
WHERE f.area_id IS NULL OR rs.id IS NOT NULL;

-- Freshness says why a repo's age keeps growing. Appended, so the view keeps
-- its columns' order.
CREATE OR REPLACE VIEW freshness AS
SELECT
    r.name                    AS repo,
    lc.sha                    AS last_recorded_sha,
    lc.committed_at           AS last_recorded_committed_at,
    now() - lc.committed_at   AS last_recorded_age,
    lr.started_at             AS last_run_started_at,
    lr.finished_at            AS last_run_finished_at,
    lr.outcome                AS last_run_outcome,
    r.skipped_at              AS skipped_at
FROM repo r
LEFT JOIN LATERAL (
    SELECT c.sha, c.committed_at
    FROM commit c
    WHERE c.repo_id = r.id
      AND EXISTS (SELECT 1 FROM commit_measure cm WHERE cm.commit_id = c.id)
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
