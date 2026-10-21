-- Which measure versions have been computed at which commit. A commit is done
-- for a measure version when its marker exists — not when it has rows, since a
-- measure may legitimately yield no value at a commit and would otherwise be
-- recomputed on every run. The collector writes a commit's values and markers
-- in one transaction.
CREATE TABLE commit_measure (
    commit_id    bigint NOT NULL REFERENCES commit (id),
    measure_id   integer NOT NULL REFERENCES measure (id),
    measured_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (commit_id, measure_id)
);
CREATE INDEX commit_measure_measure ON commit_measure (measure_id);

-- Freshness now means "the newest commit the collector finished", which the
-- markers say exactly.
CREATE OR REPLACE VIEW freshness AS
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
