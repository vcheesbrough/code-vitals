-- A repo that is gone — deleted, made private without a clone token, moved
-- without its URL being re-pointed — fails its clone, and a failed clone fails
-- the deployment, as it must: a forge outage looks exactly the same. The owner
-- *retires* such a repo (`code-vitals retire-repo`, card #586): `retired_at`
-- is when; NULL means it is not retired.
--
-- A retired repo is also skipped (the CHECK below), so everything a skipped
-- repo does holds for it: it does not count toward completing a measure
-- version, and the views show its history frozen at the versions it had
-- finished (repo_current_measure, migration 0007). What retiring adds is that
-- deployments stop cloning it (`list-repos` leaves it out), where a skipped
-- repo is still cloned each deploy to see whether its plugin step came back.
-- The first run that clones it from its URL again — its own plugin step's —
-- clears `retired_at`; it is then a skipped repo like any other, active again
-- once caught up.
ALTER TABLE repo ADD COLUMN retired_at timestamptz;
ALTER TABLE repo ADD CONSTRAINT repo_retired_is_skipped
    CHECK (retired_at IS NULL OR skipped_at IS NOT NULL);

-- Freshness says a repo is retired. Appended, so the view keeps its columns'
-- order.
CREATE OR REPLACE VIEW freshness AS
SELECT
    r.name                    AS repo,
    lc.sha                    AS last_recorded_sha,
    lc.committed_at           AS last_recorded_committed_at,
    now() - lc.committed_at   AS last_recorded_age,
    lr.started_at             AS last_run_started_at,
    lr.finished_at            AS last_run_finished_at,
    lr.outcome                AS last_run_outcome,
    r.skipped_at              AS skipped_at,
    r.retired_at              AS retired_at
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
