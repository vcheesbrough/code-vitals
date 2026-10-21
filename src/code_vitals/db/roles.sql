-- The two login roles besides the owner, and exactly what each may touch.
-- Applied by `code-vitals migrate` after the migrations on EVERY run, not as a
-- migration: a dev database restored from a prod dump with --no-acl has no
-- grants, and roles are per cluster while grants are per database. Every
-- statement is idempotent.
--
--   code_vitals_writer — the collector: reads and writes the tables, never
--                        owns, alters or drops them.
--   code_vitals_reader — Superset: SELECT on the current_* and freshness views
--                        only, never a base table.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'code_vitals_writer') THEN
        CREATE ROLE code_vitals_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'code_vitals_reader') THEN
        CREATE ROLE code_vitals_reader NOLOGIN;
    END IF;
END
$$;

-- Start from nothing, so a privilege removed here is removed from the database.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM code_vitals_writer, code_vitals_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM code_vitals_writer, code_vitals_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC, code_vitals_writer, code_vitals_reader;
GRANT USAGE ON SCHEMA public TO code_vitals_writer, code_vitals_reader;

GRANT SELECT, INSERT, UPDATE
    ON repo, commit, measure, area_ruleset, area, measurement, function_complexity, run,
       commit_measure, measure_backfill
    TO code_vitals_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO code_vitals_writer;

GRANT SELECT
    ON current_measurement, current_function_complexity, freshness
    TO code_vitals_reader;
