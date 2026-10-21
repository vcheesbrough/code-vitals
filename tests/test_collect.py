"""The collector against a real database and a real fixture repository, run as
the writer role (so the grants are proven sufficient, too)."""

import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest

from code_vitals.areas import AreaRuleset, ruleset
from code_vitals.build_info import BuildInfo
from code_vitals.collect import AreaRulesetConflict, MeasureDefinitionConflict, collect, lock_key
from code_vitals.db.migrate import migrate
from code_vitals.measures import CommitContext, Kind, Measure, Value, registered
from code_vitals.measures.churn import LinesAdded
from code_vitals.measures.complexity import FunctionHotspots, Functions
from code_vitals.measures.files import Files
from code_vitals.obs import Outcome, Trigger
from code_vitals.repos import RepoConfig, VersionFile, VersionSource

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, make_repo, pyproject, three_commit_history

BUILD = BuildInfo(version="0.4.99", revision="abc123")
FIXTURE = RepoConfig("fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),))
# These tests are about the collector, not the measures: one simple measure.
ONLY_FILES = [Files()]


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
    result = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)

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
    assert _rows(
        owner,
        "SELECT r.version, a.name FROM area a"
        " JOIN area_ruleset r ON r.id = a.ruleset_id ORDER BY a.id",
    ) == [
        (1, "tests"),
        (1, "tooling"),
    ]
    assert _rows(owner, "SELECT count(*) FROM commit_measure") == [(3,)]


def test_a_rerun_on_an_up_to_date_repo_records_nothing(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    started = time.monotonic()
    again = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)

    assert (again.backlog, again.commits_measured) == (0, 0)
    assert time.monotonic() - started < 5
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(3,)]


def test_a_new_trunk_commit_is_the_only_work(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    repo.shas.append(repo.commit("Four", {"d.py": "d\n"}))

    result = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)

    assert (result.backlog, result.commits_measured) == (1, 1)
    assert _rows(owner, "SELECT count(*) FROM commit") == [(4,)]


def test_a_run_row_records_the_build_outcome_and_trace(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    result = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    (run,) = _rows(
        owner,
        "SELECT id, trigger::text, collector_version, collector_revision, outcome::text,"
        " commits_measured, finished_at IS NOT NULL, trace_id FROM run",
    )
    assert run[:7] == (result.run_id, "push", "0.4.99", "abc123", "success", 3, True)
    assert run[7] is None  # no trace to name with telemetry off


def test_a_measure_with_no_value_is_still_done(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    empty = Empty()
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[empty])
    collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[empty])

    assert empty.calls == 3  # three commits, once each — not recomputed on the rerun
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(0,)]


def test_a_failing_commit_is_retried_and_does_not_block_later_ones(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    result = collect(
        writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[FailsAt(repo.shas[1])]
    )
    assert (result.outcome, result.commits_measured, result.commits_failed) == (
        Outcome.FAILURE,
        2,
        1,
    )
    measured = _rows(
        owner,
        "SELECT c.sha FROM commit_measure cm JOIN commit c ON c.id = cm.commit_id"
        " ORDER BY c.committed_at",
    )
    assert [m[0] for m in measured] == [repo.shas[0], repo.shas[2]]  # the later one too
    assert _rows(owner, "SELECT outcome::text, commits_measured FROM run") == [("failure", 2)]
    assert _rows(owner, "SELECT last_run_outcome::text FROM freshness") == [("failure",)]

    again = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)

    assert (again.outcome, again.backlog, again.commits_measured) == (Outcome.SUCCESS, 1, 1)
    assert _rows(owner, "SELECT count(*) FROM measurement") == [(3,)]


def test_a_failure_before_the_lock_is_recorded(db: tuple[str, str], tmp_path: Path) -> None:
    owner, writer = db
    with pytest.raises(Exception, match="no trunk"):
        collect(writer, FIXTURE, tmp_path, Trigger.PUSH, BUILD, measures=ONLY_FILES)  # not a clone
    assert _rows(owner, "SELECT outcome::text, finished_at IS NOT NULL FROM run") == [
        ("failure", True)
    ]


def test_tags_added_or_removed_after_measuring_are_recorded(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    # The release flow tags a commit only after deploying it, i.e. after the
    # push-time run measured it.
    repo.git("tag", "-a", "0.3.4", "-m", "deployed", repo.shas[2])
    repo.git("tag", "-d", "0.2.9")

    again = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)

    assert again.commits_measured == 0
    assert _rows(owner, "SELECT sha, tags FROM commit WHERE tags <> '{}'") == [
        (repo.shas[2], ["0.3.4"])
    ]


def _wait_until_lock_held(owner: str) -> None:
    """Block until some session holds the fixture repo's advisory lock."""
    deadline = time.monotonic() + 10
    with psycopg.connect(owner) as conn:
        while time.monotonic() < deadline:
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND objid = (hashtext(%s)::bigint & x'FFFFFFFF'::bigint)::oid",
                (lock_key("fixture"),),
            ).fetchone()
            if held and held[0]:
                return
            time.sleep(0.02)
    raise AssertionError("the slow run never took the repo lock")


class Slow(Files):
    """`files`, slowly: keeps the run (and its repo lock) busy."""

    def compute(self, context: CommitContext) -> Sequence[Value]:
        time.sleep(0.3)
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
    _wait_until_lock_held(owner)
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
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)

    class Renamed(Files):
        unit = "bytes"

    with pytest.raises(MeasureDefinitionConflict, match="needs a new version"):
        collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[Renamed()])


def test_freshness_reports_the_newest_finished_commit(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    assert _rows(
        owner, "SELECT repo, last_recorded_sha, last_run_outcome::text FROM freshness"
    ) == [("fixture", repo.shas[-1], "success")]


def test_real_measures_record_values_by_area_and_function_rows(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    repo = make_repo(tmp_path)
    repo.commit(
        "app",
        {
            "pyproject.toml": pyproject("0.5.0"),
            "app/main.py": "def f(x):\n    if x:\n        return 1\n    return 2\n",
            "app/test_main.py": "def test_f():\n    assert True\n",
            "e2e/tests/a.spec.ts": "test('x', () => {});\n",
        },
    )
    rules = AreaRuleset(1, (), (("e2e/**", "tests"), ("app/**", "app"), ("**", "tooling")))
    config = RepoConfig(
        "fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),), rules
    )

    result = collect(writer, config, repo.path, Trigger.BACKFILL, BUILD)

    assert result.commits_failed == 0
    values = _rows(
        owner,
        "SELECT m.name, a.name, x.facet, x.value FROM measurement x"
        " JOIN measure m ON m.id = x.measure_id LEFT JOIN area a ON a.id = x.area_id"
        " WHERE m.name IN ('lines_of_code', 'complexity_total') ORDER BY 1, 2, 3",
    )
    assert values == [
        ("complexity_total", "app", "", 3.0),  # f (2) + test_f (1): no test rules here
        ("complexity_total", "tests", "", 1.0),
        ("lines_of_code", "app", "Python", 6.0),
        ("lines_of_code", "tests", "TypeScript", 1.0),
        ("lines_of_code", "tooling", "TOML", 3.0),
    ]
    functions = _rows(
        owner,
        "SELECT a.name, f.file, f.function, f.ccn FROM function_complexity f"
        " JOIN area a ON a.id = f.area_id ORDER BY f.file",
    )
    assert ("app", "app/main.py", "f", 2) in functions
    assert _rows(owner, "SELECT count(DISTINCT measure_id) FROM commit_measure") == [
        (len(registered()),)
    ]


def test_changed_area_rules_without_a_version_bump_are_refused(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    moved = RepoConfig(
        FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, ruleset(1, ("b/**", "b"))
    )
    with pytest.raises(AreaRulesetConflict, match="need a new version"):
        collect(writer, moved, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)


def test_a_changed_definition_text_without_a_version_bump_is_refused(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)

    class Reworded(Files):
        definition = "Counted differently now."

    with pytest.raises(MeasureDefinitionConflict, match="needs a"):
        collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[Reworded()])


def _config(rules: AreaRuleset) -> RepoConfig:
    return RepoConfig(
        "fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),), rules
    )


def _app_repo(tmp_path: Path) -> FixtureRepo:
    repo = make_repo(tmp_path)
    repo.shas.append(
        repo.commit("one", {"a/x.py": "def f(x):\n    return x\n", "b/y.py": "y = 1\n"})
    )
    repo.shas.append(repo.commit("two", {"b/z.py": "def g():\n    return 1\n"}))
    return repo


def test_a_ruleset_bump_re_measures_the_repo_into_the_new_areas(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    repo = _app_repo(tmp_path)
    measures: list[Measure] = [Files(), Functions()]
    v1 = AreaRuleset(1, (), (("a/**", "a"), ("**", "tooling")))
    v2 = AreaRuleset(2, (), (("a/**", "a"), ("b/**", "b"), ("**", "tooling")))
    collect(writer, _config(v1), repo.path, Trigger.BACKFILL, BUILD, measures=measures)

    again = collect(writer, _config(v2), repo.path, Trigger.BACKFILL, BUILD, measures=measures)

    assert (again.backlog, again.commits_measured) == (2, 2)
    by_ruleset = _rows(
        owner,
        "SELECT r.version, a.name, sum(x.value) FROM measurement x"
        " JOIN measure m ON m.id = x.measure_id AND m.name = 'functions'"
        " JOIN area a ON a.id = x.area_id JOIN area_ruleset r ON r.id = a.ruleset_id"
        " GROUP BY 1, 2 ORDER BY 1, 2",
    )
    assert by_ruleset == [(1, "a", 2.0), (1, "tooling", 1.0), (2, "a", 2.0), (2, "b", 1.0)]
    # The unsplit `files` values were not duplicated by the re-measure.
    assert _rows(
        owner,
        "SELECT count(*) FROM measurement x JOIN measure m ON m.id = x.measure_id"
        " WHERE m.name = 'files'",
    ) == [(2,)]


class UnknownArea(Files):
    name = "unknown_area"
    definition = "Puts a value in an area the ruleset does not have."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return [Value(1.0, area="nowhere")]


def test_a_value_in_an_unknown_area_fails_that_commit(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    result = collect(writer, FIXTURE, repo.path, Trigger.PUSH, BUILD, measures=[UnknownArea()])
    assert (result.outcome, result.commits_failed) == (Outcome.FAILURE, 3)


def test_the_hotspot_view_shows_areas_of_the_current_ruleset_only(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    repo = _app_repo(tmp_path)
    v1 = AreaRuleset(1, (), (("a/**", "a"), ("**", "tooling")))
    collect(writer, _config(v1), repo.path, Trigger.BACKFILL, BUILD, measures=[FunctionHotspots()])
    with psycopg.connect(owner) as conn:
        conn.execute("UPDATE measure SET status = 'complete'")
        conn.execute("UPDATE area_ruleset SET status = 'complete'")
    hotspots = _rows(
        owner,
        "SELECT area, file, function, ccn FROM current_function_complexity ORDER BY sha, file",
    )
    assert ("a", "a/x.py", "f", 1) in hotspots
    assert {row[0] for row in hotspots} == {"a", "tooling"}

    with psycopg.connect(owner) as conn:  # a newer ruleset completes: v1's rows drop out
        conn.execute(
            "INSERT INTO area_ruleset (repo_id, version, rules, status)"
            " SELECT repo_id, 2, '{}', 'complete' FROM area_ruleset"
        )
    assert _rows(owner, "SELECT count(*) FROM current_function_complexity") == [(0,)]


def test_churn_per_commit_follows_the_first_parent_through_a_merge(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    repo = make_repo(tmp_path)
    c1 = repo.commit("one", {"a.py": "1\n2\n"})
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("side 1", {"b.py": "1\n2\n3\n"})
    repo.commit("side 2", {"b.py": "1\n2\n3\n4\n"})
    repo.git("switch", "-q", "main")
    c2 = repo.commit("two", {"a.py": "1\n"})
    repo.git("merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge = repo.git("rev-parse", "HEAD")
    config = _config(AreaRuleset(1, (), (("**", "tooling"),)))

    collect(writer, config, repo.path, Trigger.BACKFILL, BUILD, measures=[LinesAdded()])

    added: dict[object, object] = {
        sha: value
        for sha, value in _rows(
            owner,
            "SELECT c.sha, x.value FROM measurement x JOIN commit c ON c.id = x.commit_id",
        )
    }
    # The merge brings in the whole feature branch against its first parent;
    # the side commits are not on the first-parent history at all.
    assert added == {c1: 2.0, c2: 0.0, merge: 4.0}  # c2 only deletes a line
