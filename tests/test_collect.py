"""The collector against a real database and a real fixture repository, run as
the writer role (so the grants are proven sufficient, too)."""

import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest

from code_vitals import cli
from code_vitals import collect as collect_module
from code_vitals.areas import AreaRuleset, ruleset
from code_vitals.build_info import BuildInfo
from code_vitals.collect import (
    AreaRulesetConflict,
    MeasureDefinitionConflict,
    RepoNameConflict,
    RepoNotRegistered,
    collect,
    lock_key,
    retire_repo,
    skip_repo,
    unretire_repo,
)
from code_vitals.db.migrate import migrate
from code_vitals.gitrepo import GitError
from code_vitals.measures import CommitContext, Kind, Measure, Value, registered
from code_vitals.measures.churn import LinesAdded
from code_vitals.measures.complexity import FunctionHotspots, Functions
from code_vitals.measures.files import Files
from code_vitals.obs import Outcome, Trigger
from code_vitals.repos import RepoConfig, VersionFile, VersionSource
from code_vitals.settings import SETTINGS_ENV

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, make_repo, pyproject, three_commit_history

BUILD = BuildInfo(version="0.4.99", revision="abc123")
FIXTURE = RepoConfig("fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),))
FIXTURE_SETTINGS = (
    '{"trunk": "main", "version_sources": [{"path": "pyproject.toml", "kind": "pyproject"}]}'
)
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
    # The only repo has every trunk commit measured: the version is complete.
    assert _rows(owner, "SELECT name, version, status::text FROM measure") == [
        ("files", 1, "complete")
    ]
    assert _rows(owner, "SELECT version, status::text FROM area_ruleset") == [(1, "complete")]
    assert _rows(owner, "SELECT head_sha, run_id FROM measure_backfill") == [
        (repo.shas[-1], result.run_id)
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
    # v2 completed with the run, so the view now reads its areas only.
    assert _rows(
        owner,
        "SELECT area, sum(value) FROM current_measurement WHERE measure = 'functions'"
        " GROUP BY 1 ORDER BY 1",
    ) == [("a", 2.0), ("b", 1.0)]


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


class FilesV2(Files):
    """`files`, bumped: what a branch that changes a measure deploys."""

    version = 2
    definition = "Regular files, counted again (v2)."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return [Value(float(context.files) * 10)]


class FilesV2FailsAt(FilesV2):
    def __init__(self, sha: str) -> None:
        self.sha = sha

    def compute(self, context: CommitContext) -> Sequence[Value]:
        if context.sha == self.sha:
            raise RuntimeError("killed part way")
        return super().compute(context)


def _view(owner: str) -> list[tuple[object, ...]]:
    return _rows(
        owner,
        "SELECT repo, measure_version, value FROM current_measurement"
        " WHERE measure = 'files' ORDER BY repo, committed_at",
    )


def test_a_bumped_version_backfills_behind_the_old_one_then_switches(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    assert _view(owner) == [("fixture", 1, 1.0), ("fixture", 1, 3.0), ("fixture", 1, 2.0)]

    # The backfill dies part way: v2 has rows, but the views keep reading v1.
    interrupted = collect(
        writer,
        FIXTURE,
        repo.path,
        Trigger.BACKFILL,
        BUILD,
        measures=[FilesV2FailsAt(repo.shas[1])],
    )
    assert interrupted.outcome is Outcome.FAILURE
    assert _rows(owner, "SELECT version, status::text FROM measure ORDER BY version") == [
        (1, "complete"),
        (2, "backfilling"),
    ]
    assert {v[1] for v in _view(owner)} == {1}

    # Redeploying finishes the backfill, and the views switch to v2 whole.
    redeployed = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    assert (redeployed.outcome, redeployed.commits_measured) == (Outcome.SUCCESS, 1)
    assert _view(owner) == [("fixture", 2, 10.0), ("fixture", 2, 30.0), ("fixture", 2, 20.0)]
    # v1's rows are untouched.
    assert _rows(
        owner,
        "SELECT count(*) FROM measurement x JOIN measure m ON m.id = x.measure_id"
        " WHERE m.version = 1",
    ) == [(3,)]


def test_a_version_completes_only_once_every_repo_has_backfilled_it(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = three_commit_history(tmp_path / "first")
    second = three_commit_history(tmp_path / "second")
    other = RepoConfig("other", "main", FIXTURE.version_sources)
    for config, clone in ((FIXTURE, first), (other, second)):
        collect(writer, config, clone.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)

    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    assert _rows(owner, "SELECT status::text FROM measure WHERE version = 2") == [("backfilling",)]
    assert {(v[0], v[1]) for v in _view(owner)} == {("fixture", 1), ("other", 1)}

    collect(writer, other, second.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    assert _rows(owner, "SELECT status::text FROM measure WHERE version = 2") == [("complete",)]
    assert {(v[0], v[1]) for v in _view(owner)} == {("fixture", 2), ("other", 2)}


def _two_repos(writer: str, tmp_path: Path) -> tuple[FixtureRepo, RepoConfig, FixtureRepo]:
    """`fixture` and `other`, both caught up on files v1."""
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = three_commit_history(tmp_path / "first")
    second = three_commit_history(tmp_path / "second")
    other = RepoConfig("other", "main", FIXTURE.version_sources)
    for config, clone in ((FIXTURE, first), (other, second)):
        collect(writer, config, clone.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    return first, other, second


def _statuses(owner: str) -> list[tuple[object, ...]]:
    return _rows(owner, "SELECT version, status::text FROM measure ORDER BY version")


def _skipped(owner: str) -> list[object]:
    return [r[0] for r in _rows(owner, "SELECT name FROM repo WHERE skipped_at IS NOT NULL")]


def test_a_skipped_repo_does_not_hold_a_new_version_back_and_keeps_its_history(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    first, _other, _second = _two_repos(writer, tmp_path)

    assert skip_repo(writer, "other") is True
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])

    assert _statuses(owner) == [(1, "complete"), (2, "complete")]
    # The active repo switched to v2; the skipped one stays visible, frozen at
    # the version it had finished.
    assert _view(owner) == [
        ("fixture", 2, 10.0),
        ("fixture", 2, 30.0),
        ("fixture", 2, 20.0),
        ("other", 1, 1.0),
        ("other", 1, 3.0),
        ("other", 1, 2.0),
    ]


def test_skipping_the_last_repo_a_version_waited_for_completes_it(
    db: tuple[str, str], tmp_path: Path
) -> None:
    # A deployment backfills its repos in parallel: the skip may come last.
    owner, writer = db
    first, _other, _second = _two_repos(writer, tmp_path)
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    assert _statuses(owner) == [(1, "complete"), (2, "backfilling")]

    skip_repo(writer, "other")

    assert _statuses(owner) == [(1, "complete"), (2, "complete")]


def test_the_first_skip_is_the_one_recorded(db: tuple[str, str], tmp_path: Path) -> None:
    owner, writer = db
    _two_repos(writer, tmp_path)
    assert skip_repo(writer, "other") is True
    marked = _rows(owner, "SELECT skipped_at FROM repo WHERE name = 'other'")
    assert skip_repo(writer, "other") is False  # every later deployment
    assert _rows(owner, "SELECT skipped_at FROM repo WHERE name = 'other'") == marked
    assert skip_repo(writer, "unregistered") is False


def test_a_version_no_active_repo_backfilled_does_not_complete(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    first, _other, _second = _two_repos(writer, tmp_path)
    # v2 is registered, but its only run dies part way: no repo backfilled it.
    collect(
        writer,
        FIXTURE,
        first.path,
        Trigger.BACKFILL,
        BUILD,
        measures=[FilesV2FailsAt(first.shas[1])],
    )
    skip_repo(writer, "fixture")
    skip_repo(writer, "other")
    assert _statuses(owner) == [(1, "complete"), (2, "backfilling")]
    assert {(v[0], v[1]) for v in _view(owner)} == {("fixture", 1), ("other", 1)}


def test_a_repo_that_comes_back_is_backfilled_for_every_version_before_it_counts_again(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    first, other, second = _two_repos(writer, tmp_path)
    skip_repo(writer, "other")
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])

    # Its first run back dies part way: still skipped, still frozen at v1, and
    # a later version (v3 here) does not wait for it.
    died = collect(
        writer, other, second.path, Trigger.PUSH, BUILD, measures=[FilesV2FailsAt(second.shas[1])]
    )
    assert died.outcome is Outcome.FAILURE
    assert _skipped(owner) == ["other"]
    assert {v[1] for v in _view(owner) if v[0] == "other"} == {1}

    # The run that finishes makes it active again, with every version it
    # missed recorded at every commit, and its charts switch whole.
    back = collect(writer, other, second.path, Trigger.PUSH, BUILD, measures=[FilesV2()])
    assert (back.outcome, back.commits_measured) == (Outcome.SUCCESS, 1)
    assert _skipped(owner) == []
    assert [v for v in _view(owner) if v[0] == "other"] == [
        ("other", 2, 10.0),
        ("other", 2, 30.0),
        ("other", 2, 20.0),
    ]
    assert _rows(
        owner,
        "SELECT count(*) FROM measure_backfill b JOIN repo r ON r.id = b.repo_id"
        " WHERE r.name = 'other'",
    ) == [(2,)]

    # Active again, it holds the next version back like any other repo.
    class FilesV3(FilesV2):
        version = 3
        definition = "Regular files, counted a third time."

    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV3()])
    assert _statuses(owner)[-1] == (3, "backfilling")
    collect(writer, other, second.path, Trigger.BACKFILL, BUILD, measures=[FilesV3()])
    assert _statuses(owner)[-1] == (3, "complete")


def test_a_clean_run_of_an_older_build_does_not_reactivate_a_skipped_repo(
    db: tuple[str, str], tmp_path: Path
) -> None:
    # v2 completed without `other`; then a collector that knows only v1 runs
    # for it (a plugin of the previous :stable, a manual run of an old image).
    owner, writer = db
    first, other, second = _two_repos(writer, tmp_path)
    skip_repo(writer, "other")
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])

    old = collect(writer, other, second.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)

    # Nothing to do and nothing failed, but it lacks the version the views
    # show: still skipped, still frozen at v1 rather than blank at v2.
    assert (old.outcome, old.backlog) == (Outcome.SUCCESS, 0)
    assert _skipped(owner) == ["other"]
    assert [v for v in _view(owner) if v[0] == "other"] == [
        ("other", 1, 1.0),
        ("other", 1, 3.0),
        ("other", 1, 2.0),
    ]

    # The current build's run is what brings it back.
    collect(writer, other, second.path, Trigger.PUSH, BUILD, measures=[FilesV2()])
    assert _skipped(owner) == []
    assert {v[1] for v in _view(owner) if v[0] == "other"} == {2}


def _retired(owner: str) -> list[object]:
    return [r[0] for r in _rows(owner, "SELECT name FROM repo WHERE retired_at IS NOT NULL")]


def test_retiring_a_repo_completes_what_waited_for_it_and_keeps_its_history(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    first, _other, _second = _two_repos(writer, tmp_path)
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    assert _statuses(owner) == [(1, "complete"), (2, "backfilling")]

    retired = retire_repo(writer, "other")

    assert retired.newly is True
    assert _statuses(owner) == [(1, "complete"), (2, "complete")]
    assert (_retired(owner), _skipped(owner)) == (["other"], ["other"])
    # Frozen at the version it had finished, like a skipped repo.
    assert {(v[0], v[1]) for v in _view(owner)} == {("fixture", 2), ("other", 1)}


def test_the_first_retirement_is_kept_and_an_unknown_repo_is_refused(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    _two_repos(writer, tmp_path)
    skip_repo(writer, "other")
    skipped = _rows(owner, "SELECT skipped_at FROM repo WHERE name = 'other'")

    first = retire_repo(writer, "other")
    again = retire_repo(writer, "other")

    assert (first.newly, again.newly) == (True, False)
    assert again.retired_at == first.retired_at
    # An earlier skip keeps its time.
    assert _rows(owner, "SELECT skipped_at FROM repo WHERE name = 'other'") == skipped
    with pytest.raises(RepoNotRegistered, match="no repo named nobody is registered"):
        retire_repo(writer, "nobody")
    with pytest.raises(RepoNotRegistered, match="no repo named nobody is registered"):
        unretire_repo(writer, "nobody")


def test_unretiring_a_repo_leaves_it_skipped_until_it_is_caught_up(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    _two_repos(writer, tmp_path)
    retire_repo(writer, "other")

    assert unretire_repo(writer, "other") is True
    assert unretire_repo(writer, "other") is False
    assert (_retired(owner), _skipped(owner)) == ([], ["other"])


def test_a_retired_repo_comes_back_on_its_next_run_from_its_url(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    first, other, second = _two_repos(writer, tmp_path)
    retire_repo(writer, "other")
    collect(writer, FIXTURE, first.path, Trigger.BACKFILL, BUILD, measures=[FilesV2()])
    from_url = RepoConfig(other.name, other.trunk, other.version_sources, url=str(second.path))

    # A clean run from a local clone says nothing about its URL: it stays
    # retired, so it stays skipped too (its views read the v2 it now has).
    local = collect(writer, from_url, second.path, Trigger.MANUAL, BUILD, measures=[FilesV2()])
    assert local.outcome is Outcome.SUCCESS
    assert (_retired(owner), _skipped(owner)) == (["other"], ["other"])

    # A clone of its URL that fails leaves it retired: that is what a gone
    # repo does.
    away = second.path.rename(tmp_path / "away")
    with pytest.raises(GitError):
        collect(writer, from_url, None, Trigger.PUSH, BUILD, measures=[FilesV2()])
    assert _retired(owner) == ["other"]
    away.rename(second.path)

    # Its plugin step clones it from its URL: it exists, and is active again
    # once caught up.
    back = collect(writer, from_url, None, Trigger.PUSH, BUILD, measures=[FilesV2()])
    assert (back.outcome, back.unretired) == (Outcome.SUCCESS, True)
    assert local.unretired is False
    assert (_retired(owner), _skipped(owner)) == ([], [])
    assert {v[1] for v in _view(owner) if v[0] == "other"} == {2}


def test_cli_says_in_the_step_log_when_a_retired_repo_comes_back(
    db: tuple[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # INFO records go to OTLP only; this one reverses the owner's mark, so the
    # plugin step's log says it too.
    _, writer = db
    _first, _other, second = _two_repos(writer, tmp_path)
    retire_repo(writer, "other")
    env = {cli.DATABASE_URL_ENV: writer, SETTINGS_ENV: FIXTURE_SETTINGS}
    argv = ["collect", "--repo", "other", "--url", str(second.path), "--fetch"]

    assert cli.main(argv, env) == 0
    assert (
        "code-vitals collect: other was retired, but cloned from its URL again; no longer"
        " retired, so deployments re-measure it" in capsys.readouterr().out
    )
    assert cli.main(argv, env) == 0
    assert "was retired" not in capsys.readouterr().out


def test_cli_retires_and_unretires_a_repo(
    db: tuple[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner, writer = db
    _two_repos(writer, tmp_path)
    env = {cli.DATABASE_URL_ENV: writer}

    assert cli.main(["retire-repo", "other"], env) == 0
    assert "other retired; deployments no longer clone it" in capsys.readouterr().out
    assert cli.main(["retire-repo", "other"], env) == 0
    assert "other was already retired (since " in capsys.readouterr().out
    assert cli.main(["retire-repo", "nobody"], env) == 1
    assert "no repo named nobody is registered" in capsys.readouterr().err
    assert _retired(owner) == ["other"]

    assert cli.main(["unretire-repo", "other"], env) == 0
    assert "other is no longer retired; deployments clone it again" in capsys.readouterr().out
    assert cli.main(["unretire-repo", "other"], env) == 0
    assert "other was not retired" in capsys.readouterr().out
    assert cli.main(["unretire-repo", "nobody"], env) == 1
    assert _retired(owner) == []


def test_the_hotspot_view_keeps_a_skipped_repos_functions(
    db: tuple[str, str], tmp_path: Path
) -> None:
    owner, writer = db
    fixture = make_repo(tmp_path)
    fixture.commit(
        "Add code", {"pyproject.toml": pyproject("0.1.0"), "a.py": "def f():\n    pass\n"}
    )
    measures: list[Measure] = [FunctionHotspots()]
    collect(writer, FIXTURE, fixture.path, Trigger.BACKFILL, BUILD, measures=measures)
    before = _rows(owner, "SELECT repo, function FROM current_function_complexity")
    assert before
    skip_repo(writer, "fixture")
    assert _rows(owner, "SELECT repo, function FROM current_function_complexity") == before


class Snapshot(Files):
    """A non-recomputable measure: it can only be read at the time it runs."""

    name = "snapshot"
    recomputable = False
    definition = "Only knowable when measured."


def test_a_non_recomputable_version_starts_from_its_release(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    released_at = repo.git("show", "-s", "--format=%cI", repo.shas[1])
    with psycopg.connect(owner) as conn:  # released between the first two commits
        conn.execute(
            "INSERT INTO measure (name, version, unit, kind, definition, recomputable,"
            " released_at) VALUES (%s, 1, %s, %s, %s, false, %s)",
            (Snapshot.name, Snapshot.unit, str(Snapshot.kind), Snapshot.definition, released_at),
        )

    result = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[Snapshot()])

    assert (result.outcome, result.backlog) == (Outcome.SUCCESS, 2)
    assert [
        r[0]
        for r in _rows(
            owner,
            "SELECT c.sha FROM commit_measure cm JOIN commit c"
            " ON c.id = cm.commit_id ORDER BY c.committed_at",
        )
    ] == repo.shas[1:]
    assert _rows(owner, "SELECT status::text FROM measure") == [("complete",)]
    again = collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=[Snapshot()])
    assert again.backlog == 0


def test_without_a_clone_path_the_repo_is_fetched_from_its_url(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    fetched = RepoConfig(FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, url=str(repo.path))

    result = collect(writer, fetched, None, Trigger.PUSH, BUILD, measures=ONLY_FILES)

    assert (result.outcome, result.commits_measured) == (Outcome.SUCCESS, 3)
    # Tags come with the clone, as for a deploy's clone.
    assert _rows(owner, "SELECT tags FROM commit WHERE tags <> '{}'") == [(["0.2.9"],)]


def test_a_failed_fetch_is_recorded_on_the_run(db: tuple[str, str], tmp_path: Path) -> None:
    owner, writer = db
    missing = RepoConfig(
        FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, url=str(tmp_path / "nowhere")
    )
    with pytest.raises(GitError, match=r"git clone of .* failed"):
        collect(writer, missing, None, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    assert _rows(owner, "SELECT outcome::text FROM run") == [("failure",)]


def test_fetching_a_repo_without_a_url_is_recorded_as_a_failure(db: tuple[str, str]) -> None:
    owner, writer = db
    with pytest.raises(ValueError, match="fixture has no URL to fetch from"):
        collect(writer, FIXTURE, None, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    assert _rows(owner, "SELECT outcome::text FROM run") == [("failure",)]


def test_cli_collect_fetch_clones_the_configured_url(
    db: tuple[str, str],
    repo: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner, writer = db
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    status = cli.main(
        ["collect", "--repo", "fixture", "--url", str(repo.path), "--fetch", "--trigger", "push"],
        {cli.DATABASE_URL_ENV: writer, SETTINGS_ENV: FIXTURE_SETTINGS},
    )

    assert status == 0
    out = capsys.readouterr().out
    assert f"code-vitals collect: fixture from {repo.path} (push)" in out
    assert "fixture: 3 commit(s) measured of 3 to do" in out
    assert _rows(owner, "SELECT trigger::text, outcome::text FROM run") == [("push", "success")]
    # The repo registers itself, with where it was cloned from.
    assert _rows(owner, "SELECT name, trunk, url FROM repo") == [
        ("fixture", "main", str(repo.path))
    ]


def test_a_run_from_a_clone_keeps_the_recorded_url(
    db: tuple[str, str], repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, writer = db
    with_url = RepoConfig(FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, url=str(repo.path))
    collect(writer, with_url, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    collect(writer, FIXTURE, repo.path, Trigger.MANUAL, BUILD, measures=ONLY_FILES)
    assert _rows(owner, "SELECT url FROM repo") == [(str(repo.path),)]


def test_a_second_repo_of_the_same_name_is_refused(db: tuple[str, str], repo: FixtureRepo) -> None:
    owner, writer = db
    first = RepoConfig(FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, url=str(repo.path))
    collect(writer, first, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    before = _rows(owner, "SELECT name, trunk, url FROM repo")
    fork = RepoConfig(
        FIXTURE.name, "dev", FIXTURE.version_sources, url="https://x.example/fork.git"
    )

    with pytest.raises(RepoNameConflict) as refused:
        collect(writer, fork, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    assert f"already registered from {repo.path}" in str(refused.value)
    # The step log says how to recover when the repo really moved.
    assert (
        "If the repo itself moved, as the database owner run `UPDATE repo SET url ="
        " 'https://x.example/fork.git' WHERE name = 'fixture'`, then re-run"
    ) in str(refused.value)

    assert _rows(owner, "SELECT name, trunk, url FROM repo") == before
    # Nothing recorded for the newcomer: no run on the existing repo's row.
    assert _rows(owner, "SELECT count(*) FROM run") == [(1,)]


@pytest.mark.parametrize("suffix", ["", ".git", "/", ".git/"])
def test_the_same_repo_with_or_without_dot_git_is_not_a_conflict(
    db: tuple[str, str], repo: FixtureRepo, suffix: str
) -> None:
    owner, writer = db
    url = "https://github.com/vcheesbrough/fixture"
    with psycopg.connect(owner) as conn:
        conn.execute("INSERT INTO repo (name, trunk, url) VALUES ('fixture', 'main', %s)", (url,))
    same = RepoConfig(FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, url=url + suffix)
    collect(writer, same, repo.path, Trigger.PUSH, BUILD, measures=ONLY_FILES)
    assert _rows(owner, "SELECT url FROM repo") == [(url,)]


@pytest.mark.parametrize(
    ("settings_json", "message"),
    [
        (None, "CODE_VITALS_SETTINGS is not JSON"),
        ('{"trunk": "main", "colour": "red"}', "unknown setting(s) colour"),
    ],
)
def test_cli_collect_refuses_missing_or_invalid_settings(
    db: tuple[str, str],
    repo: FixtureRepo,
    capsys: pytest.CaptureFixture[str],
    settings_json: str | None,
    message: str,
) -> None:
    owner, writer = db
    env = {cli.DATABASE_URL_ENV: writer}
    if settings_json is not None:
        env[SETTINGS_ENV] = settings_json
    status = cli.main(["collect", "--repo", "fixture", "--clone", str(repo.path)], env)
    assert status == 2
    assert message in capsys.readouterr().err
    assert _rows(owner, "SELECT count(*) FROM run") == [(0,)]


def test_cli_collect_fetch_needs_a_url(
    db: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, writer = db
    status = cli.main(
        ["collect", "--repo", "fixture", "--fetch"],
        {cli.DATABASE_URL_ENV: writer, SETTINGS_ENV: FIXTURE_SETTINGS},
    )
    assert status == 2
    assert "--fetch needs --url" in capsys.readouterr().err
