-- Hotspot rows carry their area, so "worst functions in the web area" needs no
-- path rules in SQL. The current_function_complexity view gains it, limited
-- like current_measurement to the repo's current area ruleset.
ALTER TABLE function_complexity ADD COLUMN area_id integer REFERENCES area (id);

DROP VIEW current_function_complexity;
CREATE VIEW current_function_complexity AS
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
JOIN current_measure m ON m.id = f.measure_id
JOIN commit c ON c.id = f.commit_id
JOIN repo r ON r.id = c.repo_id
LEFT JOIN area a ON a.id = f.area_id
LEFT JOIN current_area_ruleset rs ON rs.id = a.ruleset_id AND rs.repo_id = c.repo_id
WHERE f.area_id IS NULL OR rs.id IS NOT NULL;
