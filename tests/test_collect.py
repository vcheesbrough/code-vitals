"""The collector against a real database and a real fixture repository, run as
the writer role (so the grants are proven sufficient, too)."""

import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest

from code_vitals.build_info import BuildInfo
from code_vitals.collect import MeasureDefinitionConflict, collect
from code_vitals.db.migrate import migrate
from code_vitals.measures import CommitContext, Kind, Value
from code_vitals.measures.files import Files
from code_vitals.obs import Outcome, Trigger
from code_vitals.repos import RepoConfig, VersionFile, VersionSource

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, three_commit_history

BUILD = BuildInfo(version="0.4.99", revision="abc123")
FIXTURE = RepoConfig("fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),))


@pytest.fixture
def db(fresh_db: str) -> Iterator[tuple[str, str]]:
    """(owner URL, writer URL) of a migrated database."""
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db, role_url(fresh_db, "code_vitals_writer")


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return three_commit_history(tmp_path)


def _rows(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchall()


class Empty:
    """A measure that legitimately yields no value."""

    name, version, unit, kind = "empty", 1, "things", Kind.SNAPSHOT
    recomputable, definition = True, "Never has a value."

    def __init__(self) -> None:
        self.calls = 0

    def compute(self, context: CommitContext) -> Sequence[Value]:
        self.calls += 1
        return []


class FailsAt:
    """`files`, but raising at one commit — a run that dies part way."""

    name, version, unit, kind = "files", 1, "files", Kind.SNAPSHOT
    recomputable, definition = True, Files.definition

    def __init__(self, sha: str) -> None:
        self.sha = sha

    def compute(self, context: CommitContext) -> Sequence[Value]:
        if context.sha == self.sha:
            raise RuntimeError("boom")
        return [Value(float(context.files))]


def test_first_run_records_every_trunk_commit(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    result = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD)

    assert (result.outcome, result.backlog, result.commits_measured) == (Outcome.SUCCESS, 3, 3)
    commits = _rows(
        owner,
        "SELECT sha, subject, pr_number, iteration, tags FROM commit ORDER BY committed_at",
    )
    assert [c[0] for c in commits] == repo.shas
    assert [(c[2], c[3], c[4]) for c in commits] == [
        (None, 1, []),
        (7, 2, ["0.2.9"]),
        (None, 3, []),
    ]
    values = _rows(
        owner,
        "SELECT c.sha, x.value FROM measurement x JOIN commit c ON c.id = x.commit_id"
        " ORDER BY c.committed_at",
    )
    assert [v[1] for v in values] == [1.0, 3.0, 2.0]
    assert _rows(owner, "SELECT name, version, status::text FROM measure") == [
        ("files", 1, "backfilling")
    ]
    assert _rows(owner, "SELECT count(*) FROM commit_measure") == [(3,)]


def test_a_rerun_on_an_up_to_date_repo_records_nothing(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD)
    started = time.monotonic()
    again = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD)

    assert (again.backlog, again.commits_measured) == (0, 0)
    assert time.monotonic() - started < 5
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(3,)]


def test_a_new_trunk_commit_is_the_only_work(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD)
    repo.shas.append(repo.commit("Four", {"d.py": "d\n"}))

    result = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD)

    assert (result.backlog, result.commits_measured) == (1, 1)
    assert _rows(owner, "SELECT count(*) FROM commit") == [(4,)]


def test_a_run_row_records_the_build_outcome_and_trace(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    result = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD)
    (run,) = _rows(
        owner,
        "SELECT id, trigger::text, collector_version, collector_revision, outcome::text,"
        " commits_measured, finished_at IS NOT NULL, trace_id FROM run",
    )
    assert run[:7] == (result.run_id, "push", "0.4.99", "abc123", "success", 3, True)
    assert isinstance(run[7], str) and len(run[7]) == 32  # all zeros with telemetry off


def test_a_measure_with_no_value_is_still_done(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    empty = Empty()
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[empty])
    collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[empty])

    assert empty.calls == 3  # three commits, once each — not recomputed on the rerun
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(0,)]


def test_a_run_killed_part_way_is_finished_by_the_next(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    with pytest.raises(RuntimeError, match="boom"):
        collect(
            writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[FailsAt(repo.shas[2])]
        )
    assert _rows(owner, "SELECT count(*) FROM commit_measure") == [(2,)]
    assert _rows(owner, "SELECT outcome::text, commits_measured FROM run") == [("failure", 2)]

    result = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD)

    assert (result.backlog, result.commits_measured) == (1, 1)
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(3,)]


class Slow(Files):
    """`files`, slowly: keeps the run (and its repo lock) busy."""

    def compute(self, context: CommitContext) -> Sequence[Value]:
        time.sleep(0.4)
        return super().compute(context)


def test_an_overlapping_run_waits_for_the_lock_then_finds_nothing(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    finished: list[tuple[str, int, int, float]] = []

    def run(label: str, measure: Files) -> None:
        r = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[measure])
        finished.append((label, r.backlog, r.commits_measured, time.monotonic()))

    slow = threading.Thread(target=run, args=("slow", Slow()))
    slow.start()
    time.sleep(0.3)  # the slow run holds the lock by now (3 commits x 0.4 s)
    second = threading.Thread(target=run, args=("second", Files()))
    second.start()
    slow.join(30)
    second.join(30)

    by_label = {label: (backlog, measured, at) for label, backlog, measured, at in finished}
    assert by_label["slow"][:2] == (3, 3)
    assert by_label["second"][:2] == (0, 0), "the second run waited, then found nothing"
    assert by_label["second"][2] >= by_label["slow"][2]
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(3,)]


def test_a_changed_definition_without_a_version_bump_is_refused(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD)

    class Renamed(Files):
        unit = "bytes"

    with pytest.raises(MeasureDefinitionConflict, match="needs a new version"):
        collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[Renamed()])


def test_freshness_reports_the_newest_finished_commit(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD)
    assert _rows(
        owner, "SELECT repo, last_recorded_sha, last_run_outcome::text FROM freshness"
    ) == [("fixture", repo.shas[-1], "success")]
