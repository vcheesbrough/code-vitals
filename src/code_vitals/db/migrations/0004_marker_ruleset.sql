-- A commit is done for a measure version *under an area ruleset*: bumping a
-- repo's ruleset version re-measures that repo's history into the new areas,
-- instead of leaving the new ruleset with no values. Unsplit values are not
-- duplicated (their key has no area, so the re-measure's insert is a no-op).
ALTER TABLE commit_measure ADD COLUMN ruleset_id integer REFERENCES area_ruleset (id);
UPDATE commit_measure cm
SET ruleset_id = (
    SELECT min(rs.id) FROM area_ruleset rs JOIN commit c ON c.repo_id = rs.repo_id
    WHERE c.id = cm.commit_id
);
DELETE FROM commit_measure WHERE ruleset_id IS NULL;
ALTER TABLE commit_measure ALTER COLUMN ruleset_id SET NOT NULL;
ALTER TABLE commit_measure DROP CONSTRAINT commit_measure_pkey;
ALTER TABLE commit_measure ADD PRIMARY KEY (commit_id, measure_id, ruleset_id);
