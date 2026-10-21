-- A repo has finished backfilling a measure version: a collector run with no
-- failed commit had measured every trunk commit up to the head it started
-- from. A measure version turns `complete` once every repo has a row, in the
-- same statement for every version a run finished, so the current_* views
-- switch to new versions all at once.
CREATE TABLE measure_backfill (
    repo_id       smallint NOT NULL REFERENCES repo (id),
    measure_id    integer NOT NULL REFERENCES measure (id),
    head_sha      text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    run_id        bigint NOT NULL REFERENCES run (id),
    completed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, measure_id)
);
CREATE INDEX measure_backfill_measure ON measure_backfill (measure_id);

-- Values of a non-recomputable measure start at its release; a chart marks the
-- break with these two columns. Appended, so the view keeps its columns' order.
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
JOIN current_measure m ON m.id = x.measure_id
JOIN commit c ON c.id = x.commit_id
JOIN repo r ON r.id = c.repo_id
LEFT JOIN area a ON a.id = x.area_id
LEFT JOIN current_area_ruleset rs ON rs.id = a.ruleset_id AND rs.repo_id = c.repo_id
WHERE x.area_id IS NULL OR rs.id IS NOT NULL;
