"""The deployment checks, against a real database and fixture repository, run
as the writer like the deployment's check steps."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from code_vitals import checks, cli
from code_vitals.areas import ruleset
from code_vitals.build_info import BuildInfo
from code_vitals.checks import (
    check_repo,
    check_status,
    fixed_value_measures,
    load_expected,
    render,
)
from code_vitals.collect import collect
from code_vitals.db.migrate import migrate
from code_vitals.measures import Measure, registered
from code_vitals.measures.files import Files
from code_vitals.obs import Trigger
from code_vitals.repos import REPOS, RepoConfig, VersionFile, VersionSource

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, three_commit_history
from .test_collect import Empty, Snapshot

BUILD = BuildInfo(version="0.7.99", revision="abc123")
FIXTURE = RepoConfig("fixture", "main", (VersionSource("pyproject.toml", VersionFile.PYPROJECT),))
ONLY_FILES: list[Measure] = [Files()]


@pytest.fixture
def db(fresh_db: str) -> Iterator[tuple[str, str]]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db, role_url(fresh_db, "code_vitals_writer")


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return three_commit_history(tmp_path)


def _table(repo: FixtureRepo, files_at_second: str = "3", version: int = 1) -> str:
    return f"""
[fixture]
ruleset = 1

[fixture.commits.{repo.shas[1]}.files]
version = {version}
"-" = {files_at_second}
"""


def _collected(writer: str, repo: FixtureRepo) -> None:
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)


def test_a_collected_repo_matching_the_table_passes(db: tuple[str, str], repo: FixtureRepo) -> None:
    _, writer = db
    _collected(writer, repo)
    expected = load_expected(_table(repo))
    problems = check_repo(writer, FIXTURE, repo.path, BUILD, measures=ONLY_FILES, expected=expected)
    assert problems == []


def test_a_wrong_fixed_value_fails_and_prints_the_recorded_entry(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    _collected(writer, repo)
    expected = load_expected(_table(repo, files_at_second="4"))

    problems = check_repo(writer, FIXTURE, repo.path, BUILD, measures=ONLY_FILES, expected=expected)

    assert len(problems) == 1
    assert "files v1: - expected 4 got 3" in problems[0]
    # The recorded entry, ready to paste, parses back to what was recorded.
    pasted = problems[0].split("paste if the change is intended:\n", 1)[1]
    assert load_expected("[fixture]\nruleset = 1\n" + pasted) == load_expected(_table(repo))


def test_a_version_bump_without_a_table_update_fails(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    _collected(writer, repo)
    problems = check_repo(
        writer,
        FIXTURE,
        repo.path,
        BUILD,
        measures=ONLY_FILES,
        expected=load_expected(_table(repo, version=2)),
    )
    assert len(problems) == 1
    assert "files: the table has v2, this build v1" in problems[0]


def test_a_ruleset_bump_without_a_table_update_fails(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    bumped = RepoConfig(
        FIXTURE.name, FIXTURE.trunk, FIXTURE.version_sources, ruleset(2, ("b/**", "b"))
    )
    collect(writer, bumped, repo.path, Trigger.BACKFILL, BUILD, measures=ONLY_FILES)
    problems = check_repo(
        writer, bumped, repo.path, BUILD, measures=ONLY_FILES, expected=load_expected(_table(repo))
    )
    assert problems == [
        "expected_values.toml was written for fixture's area ruleset v1; this build has v2:"
        " update the table"
    ]


def test_a_registered_measure_missing_from_the_table_fails(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    measures: list[Measure] = [Files(), Empty()]
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=measures)
    problems = check_repo(
        writer, FIXTURE, repo.path, BUILD, measures=measures, expected=load_expected(_table(repo))
    )
    assert len(problems) == 1
    assert "empty v1 is not in the table" in problems[0]
    # A measure with no value is listed as a bare entry.
    assert f"[fixture.commits.{repo.shas[1]}.empty]\nversion = 1" in problems[0]


def test_a_second_catch_up_with_work_left_fails(db: tuple[str, str], repo: FixtureRepo) -> None:
    _, writer = db
    _collected(writer, repo)  # without `empty`, which check_repo then finds to do
    measures: list[Measure] = [Files(), Empty()]
    table = _table(repo) + f"\n[fixture.commits.{repo.shas[1]}.empty]\nversion = 1\n"
    problems = check_repo(
        writer, FIXTURE, repo.path, BUILD, measures=measures, expected=load_expected(table)
    )
    assert len(problems) == 1
    assert problems[0].startswith("a second catch-up of fixture had 3 commit(s) to do")


def test_a_fixed_commit_that_was_never_measured_fails(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    _collected(writer, repo)
    table = _table(repo).replace(repo.shas[1], "f" * 40)
    problems = check_repo(
        writer, FIXTURE, repo.path, BUILD, measures=ONLY_FILES, expected=load_expected(table)
    )
    assert problems == [f"fixture {'f' * 40}: files v1 is not recorded"]


def test_a_repo_without_fixed_commits_fails(db: tuple[str, str], repo: FixtureRepo) -> None:
    _, writer = db
    _collected(writer, repo)
    problems = check_repo(writer, FIXTURE, repo.path, BUILD, measures=ONLY_FILES, expected={})
    assert problems == ["expected_values.toml has no fixed commits for fixture"]


def test_a_non_recomputable_measure_is_not_pinned_at_the_fixed_commits(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    _, writer = db
    # Released now, so never recorded at the fixture's (earlier) commits; nor
    # could the table hold a value every database agrees on.
    measures: list[Measure] = [Files(), Snapshot()]
    collect(writer, FIXTURE, repo.path, Trigger.BACKFILL, BUILD, measures=measures)
    problems = check_repo(
        writer, FIXTURE, repo.path, BUILD, measures=measures, expected=load_expected(_table(repo))
    )
    assert problems == []
    assert check_status(writer, [FIXTURE], measures=measures) == []


def test_status_passes_once_every_repo_is_collected(db: tuple[str, str], repo: FixtureRepo) -> None:
    _, writer = db
    _collected(writer, repo)
    assert check_status(writer, [FIXTURE], measures=ONLY_FILES) == []


def test_status_fails_on_incomplete_versions_and_failed_or_missing_runs(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    _collected(writer, repo)
    with psycopg.connect(owner) as conn:
        conn.execute("UPDATE measure SET status = 'backfilling'")
        conn.execute("UPDATE area_ruleset SET status = 'backfilling'")
        conn.execute(
            "INSERT INTO run (repo_id, trigger, collector_version, collector_revision, outcome)"
            " SELECT id, 'push', '0.7.1', 'def', 'failure' FROM repo"
        )
    absent = RepoConfig("absent", "main", FIXTURE.version_sources)

    problems = check_status(writer, [FIXTURE, absent], measures=[Files(), Empty()])

    assert problems[:3] == [
        "measure files v1 is backfilling, not complete",
        "measure empty v1 is not registered, not complete",
        "fixture's area ruleset v1 is backfilling, not complete",
    ]
    assert problems[3].startswith("fixture's last run (") and problems[3].endswith(") is failure")
    assert problems[4:] == [
        "absent's area ruleset v1 is not registered, not complete",
        "absent has no collector run",
    ]


def test_the_bundled_table_covers_every_repo_and_measure_this_build_has() -> None:
    table = load_expected()
    measures = fixed_value_measures(registered())
    assert set(table) == set(REPOS)
    for name, repo in REPOS.items():
        assert table[name].ruleset == repo.areas.version
        assert len(table[name].commits) >= 2
        for by_name in table[name].commits.values():
            assert {n: e.version for n, e in by_name.items()} == {
                m.name: m.version for m in measures
            }


def test_render_round_trips_areas_facets_and_fractions() -> None:
    values = {"files": {(None, ""): 3.0, ("web", "Rust"): 1.5, (None, "kind"): 2.0}}
    text = "[r]\nruleset = 1\n" + render("r", "a" * 40, [Files()], values)
    assert load_expected(text)["r"].commits["a" * 40]["files"].values == values["files"]


def test_cli_check_repo_fails_the_step_on_a_problem(
    db: tuple[str, str],
    repo: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, writer = db
    _collected(writer, repo)
    wrong = load_expected(_table(repo, files_at_second="4"))
    monkeypatch.setattr(checks, "load_expected", lambda: wrong)
    monkeypatch.setattr(checks, "registered", lambda: ONLY_FILES)

    status = cli.main(
        ["check-repo", "--repo", "fixture", "--clone", str(repo.path)],
        {cli.DATABASE_URL_ENV: writer},
        repos={"fixture": FIXTURE},
    )

    assert status == 1
    err = capsys.readouterr().err
    assert "files v1: - expected 4 got 3" in err
    assert "code-vitals check-repo: 1 problem(s)" in err


def test_cli_check_status_passes(
    db: tuple[str, str],
    repo: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, writer = db
    _collected(writer, repo)
    monkeypatch.setattr(checks, "registered", lambda: ONLY_FILES)
    status = cli.main(["check-status"], {cli.DATABASE_URL_ENV: writer}, repos={"fixture": FIXTURE})
    assert (status, capsys.readouterr().out.splitlines()[-1]) == (0, "code-vitals check-status: ok")
