"""Retiring a measure version, against a real database."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from code_vitals import cli
from code_vitals.build_info import BuildInfo
from code_vitals.collect import collect
from code_vitals.db.migrate import migrate
from code_vitals.measures import Measure
from code_vitals.measures.files import Files
from code_vitals.obs import Trigger
from code_vitals.retire import RetireRefused, retire

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import three_commit_history
from .test_collect import FIXTURE, FilesV2

BUILD = BuildInfo(version="0.7.99", revision="abc123")
# The build after a bump: it registers v2 only, so v1 may be retired.
BUMPED: list[Measure] = [FilesV2()]


@pytest.fixture
def db(fresh_db: str, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """(owner, writer) of a database holding `files` v1 and v2, both complete."""
    migrate(fresh_db, TEST_PASSWORDS)
    writer = role_url(fresh_db, "code_vitals_writer")
    repo = three_commit_history(tmp_path)
    for measures in ([Files()], BUMPED):
        collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=measures)
    yield fresh_db, writer


def _rows(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchall()


def _counts(url: str) -> list[tuple[object, ...]]:
    return _rows(
        url,
        "SELECT m.version, m.status::text,"
        " (SELECT count(*) FROM measurement x WHERE x.measure_id = m.id),"
        " (SELECT count(*) FROM commit_measure cm WHERE cm.measure_id = m.id)"
        " FROM measure m ORDER BY m.version",
    )


def test_retiring_keeps_the_rows_by_default(db: tuple[str, str]) -> None:
    owner, writer = db
    done = retire(writer, "files", 1, measures=BUMPED)
    assert (done.previous_status, done.rows_deleted) == ("complete", 0)
    assert _counts(owner) == [(1, "retired", 3, 3), (2, "complete", 3, 3)]


def test_retiring_with_delete_rows_deletes_them_as_the_owner(db: tuple[str, str]) -> None:
    owner, _ = db
    done = retire(owner, "files", 1, delete_rows=True, measures=BUMPED)
    assert done.rows_deleted == 6
    assert _counts(owner) == [(1, "retired", 0, 0), (2, "complete", 3, 3)]


def test_the_writer_may_not_delete_rows(db: tuple[str, str]) -> None:
    owner, writer = db
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        retire(writer, "files", 1, delete_rows=True, measures=BUMPED)
    assert _counts(owner)[0] == (1, "complete", 3, 3)  # rolled back whole


def test_a_version_this_build_registers_is_refused(db: tuple[str, str]) -> None:
    owner, writer = db
    with pytest.raises(RetireRefused, match="registered by this build"):
        retire(writer, "files", 2, measures=BUMPED)
    assert _counts(owner)[1][1] == "complete"


def test_an_unknown_version_is_refused(db: tuple[str, str]) -> None:
    _, writer = db
    with pytest.raises(RetireRefused, match="no measure files v9"):
        retire(writer, "files", 9, measures=BUMPED)


def test_cli_retire(db: tuple[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    owner, _ = db
    # This build registers files v1, so the CLI refuses it...
    assert cli.main(["retire", "files", "1"], {cli.DATABASE_URL_ENV: owner}) == 1
    assert "registered by this build" in capsys.readouterr().err
    # ...but retires v2, which it does not.
    assert cli.main(["retire", "files", "2"], {cli.DATABASE_URL_ENV: owner}) == 0
    assert "files v2 retired (was complete); 0 row(s) deleted" in capsys.readouterr().out
