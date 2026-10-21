"""The star schema's views and role boundaries, against a real database."""

from collections.abc import Iterator

import psycopg
import pytest

from code_vitals.db.migrate import migrate

from .conftest import TEST_PASSWORDS, role_url

SHA_1 = "1" * 40
SHA_2 = "2" * 40


@pytest.fixture
def db(fresh_db: str) -> Iterator[str]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db


def _seed(url: str) -> None:
    """Two commits; lines_of_code v1 complete and v2 backfilling; two area rulesets."""
    with psycopg.connect(url) as conn:
        conn.execute("INSERT INTO repo (id, name, trunk) VALUES (1, 'bored', 'main')")
        conn.execute(
            "INSERT INTO commit (id, repo_id, sha, committed_at, author, subject, iteration)"
            " VALUES (1, 1, %s, '2026-10-01T10:00Z', 'a', 'one (#1)', 1),"
            "        (2, 1, %s, '2026-10-08T10:00Z', 'a', 'two (#2)', 2)",
            (SHA_1, SHA_2),
        )
        conn.execute(
            "INSERT INTO measure (id, name, version, unit, kind, definition, recomputable, status)"
            " VALUES (1, 'lines_of_code', 1, 'lines', 'snapshot', 'v1', true, 'complete'),"
            "        (2, 'lines_of_code', 2, 'lines', 'snapshot', 'v2', true, 'backfilling')"
        )
        conn.execute(
            "INSERT INTO area_ruleset (id, repo_id, version, rules, status)"
            " VALUES (1, 1, 1, '{}', 'complete'), (2, 1, 2, '{}', 'backfilling')"
        )
        conn.execute(
            "INSERT INTO area (id, ruleset_id, name) VALUES (1, 1, 'backend'), (2, 2, 'backend')"
        )
        conn.execute(
            "INSERT INTO measurement (commit_id, measure_id, area_id, facet, value) VALUES"
            " (1, 1, 1, 'Rust', 100), (2, 1, 1, 'Rust', 120),"  # current version, current areas
            " (2, 1, 2, 'Rust', 999),"  # current version, area of a backfilling ruleset
            " (2, 2, 1, 'Rust', 777),"  # backfilling measure version
            " (2, 1, NULL, '', 5)"  # unsplit row
        )
        # The collector's done-markers (freshness reads them).
        conn.execute(
            "INSERT INTO commit_measure (commit_id, measure_id, ruleset_id)"
            " VALUES (1, 1, 1), (2, 1, 1)"
        )


def test_current_measurement_shows_only_complete_versions_and_current_areas(db: str) -> None:
    _seed(db)
    with psycopg.connect(db) as conn:
        rows = conn.execute(
            "SELECT sha, measure_version, area, facet, value, week, iteration"
            " FROM current_measurement ORDER BY sha, value"
        ).fetchall()
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        (SHA_1, 1, "backend", "Rust", 100.0),
        (SHA_2, 1, None, "", 5.0),
        (SHA_2, 1, "backend", "Rust", 120.0),
    ]
    assert str(rows[0][5]) == "2026-09-28"  # ISO week starts on Monday
    assert rows[0][6] == 1


def test_the_view_switches_when_the_new_version_completes(db: str) -> None:
    _seed(db)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE measure SET status = 'complete' WHERE id = 2")
        values = [r[0] for r in conn.execute("SELECT value FROM current_measurement")]
    assert values == [777.0]


def test_measurement_is_unique_over_all_but_the_value(db: str) -> None:
    _seed(db)
    with psycopg.connect(db) as conn:
        inserted = conn.execute(
            "INSERT INTO measurement (commit_id, measure_id, area_id, facet, value)"
            " VALUES (2, 1, NULL, '', 6) ON CONFLICT ON CONSTRAINT measurement_key DO NOTHING"
        ).rowcount
    assert inserted == 0


def test_freshness_reports_the_newest_recorded_commit_and_last_run(db: str) -> None:
    _seed(db)
    with psycopg.connect(db) as conn:
        conn.execute("INSERT INTO repo (id, name, trunk) VALUES (2, 'v-note', 'master')")
        conn.execute(
            "INSERT INTO run (repo_id, trigger, collector_version, collector_revision, outcome)"
            " VALUES (1, 'push', '0.2.5', 'abc', 'success')"
        )
        rows = conn.execute(
            "SELECT repo, last_recorded_sha, last_run_outcome FROM freshness ORDER BY repo"
        ).fetchall()
    assert rows == [("bored", SHA_2, "success"), ("v-note", None, None)]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM measurement",
        "SELECT * FROM commit",
        "SELECT * FROM current_measure",
        "SELECT * FROM commit_measure",
        "INSERT INTO repo (name, trunk) VALUES ('x', 'main')",
    ],
)
def test_reader_cannot_read_base_tables_or_write(db: str, statement: str) -> None:
    with (
        psycopg.connect(role_url(db, "code_vitals_reader")) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


def test_reader_can_read_the_published_views(db: str) -> None:
    _seed(db)
    with psycopg.connect(role_url(db, "code_vitals_reader")) as conn:
        assert len(conn.execute("SELECT * FROM current_measurement").fetchall()) == 3
        conn.execute("SELECT * FROM current_function_complexity").fetchall()
        conn.execute("SELECT * FROM freshness").fetchall()


def test_writer_reads_and_writes_tables(db: str) -> None:
    with psycopg.connect(role_url(db, "code_vitals_writer")) as conn:
        conn.execute("INSERT INTO repo (name, trunk) VALUES ('bored', 'main')")
        conn.execute(
            "INSERT INTO run (repo_id, trigger, collector_version, collector_revision)"
            " SELECT id, 'push', '0.2.5', 'abc' FROM repo"
        )
        conn.execute("UPDATE run SET outcome = 'success'")
        assert conn.execute("SELECT count(*) FROM run").fetchone() == (1,)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE measurement",
        "ALTER TABLE measurement ADD COLUMN x int",
        "TRUNCATE measurement",
        "CREATE TABLE sneaky (id int)",
        "DELETE FROM measurement",
    ],
)
def test_writer_cannot_drop_alter_or_create(db: str, statement: str) -> None:
    with (
        psycopg.connect(role_url(db, "code_vitals_writer")) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)
