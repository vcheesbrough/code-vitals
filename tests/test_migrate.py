import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from code_vitals.db import migrate as migrate_module
from code_vitals.db.migrate import bundled_migrations, load_migrations, migrate

from .conftest import TEST_PASSWORDS, role_url

# --- migration discovery (no database) --------------------------------------


def test_bundled_migrations_are_numbered_from_one_without_gaps() -> None:
    migrations = bundled_migrations()
    assert [m.number for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "0001_star_schema.sql"


def test_a_gap_in_the_numbering_is_refused(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="without gaps"):
        load_migrations(tmp_path)


def test_a_misnamed_sql_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "2_b.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match=r"NNNN_name\.sql"):
        load_migrations(tmp_path)


def test_an_unknown_login_role_is_refused() -> None:
    with pytest.raises(ValueError, match="no such login role"):
        migrate("postgresql://unused", {"postgres": "x"}, migrations=[])


# --- against a real database ------------------------------------------------


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {row[0] for row in rows}


def test_migrations_apply_from_empty_and_rerun_applies_nothing(fresh_db: str) -> None:
    assert migrate(fresh_db, TEST_PASSWORDS) == [m.name for m in bundled_migrations()]
    tables = _tables(fresh_db)
    for expected in (
        "repo",
        "commit",
        "measure",
        "area_ruleset",
        "area",
        "measurement",
        "function_complexity",
        "run",
        "current_measurement",
        "current_function_complexity",
        "freshness",
        "commit_measure",
        "schema_migrations",
    ):
        assert expected in tables

    assert migrate(fresh_db, TEST_PASSWORDS) == []


def test_concurrent_runs_apply_each_migration_once(fresh_db: str) -> None:
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(migrate(fresh_db))
        except BaseException as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(len(r) for r in results) == [0, 0, len(bundled_migrations())]


def test_a_failing_migration_rolls_back_and_is_not_recorded(fresh_db: str, tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok (id int);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE half (id int); SELECT nonsense_fn();")
    migrations = load_migrations(tmp_path)

    with pytest.raises(psycopg.Error):
        migrate(fresh_db, migrations=migrations)

    tables = _tables(fresh_db)
    assert "ok" in tables
    assert "half" not in tables
    with psycopg.connect(fresh_db) as conn:
        recorded = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
    assert recorded == ["0001_ok.sql"]


def test_grants_are_reapplied_on_a_database_that_lost_them(fresh_db: str) -> None:
    # A dev database restored from a prod dump with --no-acl has the schema and
    # the migration records but no grants; the next migrate must restore them.
    migrate(fresh_db, TEST_PASSWORDS)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM code_vitals_reader")

    assert migrate(fresh_db, TEST_PASSWORDS) == []

    with psycopg.connect(role_url(fresh_db, "code_vitals_reader")) as conn:
        conn.execute("SELECT * FROM freshness").fetchall()


def test_cli_migrate_requires_a_database_url(capsys: pytest.CaptureFixture[str]) -> None:
    from code_vitals import cli

    assert cli.main(["migrate"], environ={}) == 2
    assert "CODE_VITALS_DATABASE_URL is required" in capsys.readouterr().err


def test_cli_migrate_applies_and_sets_logins(
    fresh_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from code_vitals import cli

    environ = {
        "CODE_VITALS_DATABASE_URL": fresh_db,
        "CODE_VITALS_WRITER_PASSWORD": TEST_PASSWORDS["code_vitals_writer"],
        "CODE_VITALS_READER_PASSWORD": TEST_PASSWORDS["code_vitals_reader"],
    }
    assert cli.main(["migrate"], environ=environ) == 0
    out = capsys.readouterr().out
    assert "applied 0001_star_schema.sql" in out
    assert "logins set for 2 role(s)" in out
    with psycopg.connect(role_url(fresh_db, "code_vitals_writer")) as conn:
        conn.execute("SELECT 1 FROM repo").fetchall()


def test_lock_key_fits_a_bigint() -> None:
    assert 0 < migrate_module.MIGRATE_LOCK_KEY < 2**63


class _RecordingConnection:
    """Records each statement and whether it ran inside a transaction."""

    def __init__(self) -> None:
        self.statements: list[tuple[int, str]] = []
        self._transaction = 0
        self._depth = 0

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self._transaction += 1
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def execute(self, statement: object) -> None:
        text = statement if isinstance(statement, str) else repr(statement)
        self.statements.append((self._transaction if self._depth else 0, text))


def test_the_password_statement_runs_with_statement_logging_off() -> None:
    conn = _RecordingConnection()

    migrate_module._set_login_password(conn, "code_vitals_reader", "s3cret")  # type: ignore[arg-type]

    transactions = {tx for tx, _ in conn.statements}
    assert transactions == {1}, "everything must run in one transaction"
    texts = [text for _, text in conn.statements]
    alter = next(i for i, t in enumerate(texts) if "ALTER ROLE" in t)
    for setting in (
        "SET LOCAL log_statement = 'none'",
        "SET LOCAL log_min_error_statement = 'panic'",
        "SET LOCAL log_min_duration_statement = -1",
    ):
        assert setting in texts[:alter], f"{setting} must precede ALTER ROLE"


def test_an_empty_password_is_refused() -> None:
    with pytest.raises(ValueError, match="empty password"):
        migrate_module._set_login_password(_RecordingConnection(), "code_vitals_reader", "")  # type: ignore[arg-type]
