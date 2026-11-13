"""A deployment's backfill over the registered repos, against a real database
and fixture repositories whose own workflow file holds their settings."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from code_vitals import backfill as backfill_module
from code_vitals import checks, cli
from code_vitals import collect as collect_module
from code_vitals.backfill import Outcome, RegisteredRepo, backfill_repo, registered_repos
from code_vitals.build_info import BuildInfo
from code_vitals.db.migrate import bundled_migrations, migrate
from code_vitals.gitrepo import Clone
from code_vitals.measures import Measure
from code_vitals.measures.files import Files
from code_vitals.settings import WORKFLOW_PATH, SettingsError

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, three_commit_history

BUILD = BuildInfo(version="0.13.99", revision="abc123")
ONLY_FILES: list[Measure] = [Files()]
WORKFLOW = """when:
  - event: [push, manual]
    branch: main
steps:
  code-vitals:
    image: registry.desync.link/code-vitals-plugin:stable
    failure: ignore
    settings:
      trunk: main
      version_sources: [{path: pyproject.toml, kind: pyproject}]
      areas:
        version: 2
        rules: [{glob: "b/**", area: bee}]
      writer_password: {from_secret: code_vitals_prod_writer_password}
"""


@pytest.fixture
def db(fresh_db: str) -> Iterator[tuple[str, str]]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db, role_url(fresh_db, "code_vitals_writer")


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    """The three-commit history, with the plugin step added at the head."""
    fixture = three_commit_history(tmp_path)
    fixture.shas.append(fixture.commit("Measure me", {WORKFLOW_PATH: WORKFLOW}))
    return fixture


@pytest.fixture
def bare(tmp_path: Path) -> FixtureRepo:
    """A repo with no plugin step."""
    (tmp_path / "bare").mkdir()
    return three_commit_history(tmp_path / "bare")


def _rows(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchall()


def _registered(owner: str, *repos: tuple[str, str | None, str]) -> None:
    with psycopg.connect(owner) as conn:
        for name, url, trunk in repos:
            conn.execute(
                "INSERT INTO repo (name, url, trunk) VALUES (%s, %s, %s)", (name, url, trunk)
            )


def test_the_registered_repos_are_listed_with_their_urls(db: tuple[str, str]) -> None:
    owner, writer = db
    _registered(owner, ("v-note", "https://x/v.git", "master"), ("old", None, "main"))
    assert registered_repos(writer) == [
        RegisteredRepo("old", None, "main"),
        RegisteredRepo("v-note", "https://x/v.git", "master"),
    ]


def test_settings_are_read_from_the_workflow_at_the_trunk_head(repo: FixtureRepo) -> None:
    found = backfill_module.settings_at_trunk(Clone(repo.path), "main")
    assert found is not None
    assert found.areas.version == 2
    assert found.areas.area_of("b/c.py") == "bee"
    # Not at an earlier commit: the head decides.
    repo.git("checkout", "-q", "-b", "old", repo.shas[2])
    assert backfill_module.settings_at_trunk(Clone(repo.path), "old") is None


def test_the_yaml_extension_is_read_too(repo: FixtureRepo) -> None:
    repo.git("mv", WORKFLOW_PATH, ".woodpecker/code-vitals.yaml")
    repo.git("commit", "-q", "-m", "Rename")
    found = backfill_module.settings_at_trunk(Clone(repo.path), "main")
    assert found is not None and found.areas.version == 2


def test_a_plugin_step_in_another_workflow_file_is_not_read(repo: FixtureRepo) -> None:
    # The plugin refuses to run there (test_plugin), so this is never a repo
    # that registered and is then skipped unnoticed.
    repo.git("mv", WORKFLOW_PATH, ".woodpecker/build.yml")
    repo.git("commit", "-q", "-m", "Move")
    assert backfill_module.settings_at_trunk(Clone(repo.path), "main") is None


def test_a_repo_with_the_plugin_step_is_collected_and_checked(
    db: tuple[str, str], repo: FixtureRepo
) -> None:
    owner, writer = db
    result = backfill_repo(
        writer, RegisteredRepo("fixture", str(repo.path), "main"), BUILD, measures=ONLY_FILES
    )

    assert result.outcome is Outcome.CHECKED
    assert result.collected is not None and result.collected.commits_measured == 4
    assert result.problems == ()  # no fixed values for a new repo; ruleset complete
    assert _rows(owner, "SELECT name, trunk, url FROM repo") == [
        ("fixture", "main", str(repo.path))
    ]
    # Collected with the repo's own settings: its area ruleset v2 and areas.
    assert _rows(owner, "SELECT version, status::text FROM area_ruleset") == [(2, "complete")]
    assert ("bee",) in _rows(owner, "SELECT name FROM area")
    assert _rows(owner, "SELECT trigger::text, outcome::text FROM run ORDER BY id") == [
        ("backfill", "success"),
        ("backfill", "success"),  # check_repo's second catch-up
    ]


def test_a_repo_without_the_plugin_step_is_skipped(db: tuple[str, str], bare: FixtureRepo) -> None:
    owner, writer = db
    _registered(owner, ("bare", str(bare.path), "main"))
    result = backfill_repo(
        writer, RegisteredRepo("bare", str(bare.path), "main"), BUILD, measures=ONLY_FILES
    )
    assert result.outcome is Outcome.SKIPPED
    assert _rows(owner, "SELECT count(*) FROM run") == [(0,)]


def test_invalid_settings_fail_the_repo(db: tuple[str, str], repo: FixtureRepo) -> None:
    _, writer = db
    repo.commit("Break it", {WORKFLOW_PATH: WORKFLOW.replace("trunk: main", "trunk: ''")})
    with pytest.raises(SettingsError, match="trunk must be a branch name"):
        backfill_repo(writer, RegisteredRepo("fixture", str(repo.path), "main"), BUILD)


def test_a_repo_without_a_url_cannot_be_backfilled(db: tuple[str, str]) -> None:
    _, writer = db
    with pytest.raises(ValueError, match="old has no URL to clone"):
        backfill_repo(writer, RegisteredRepo("old", None, "main"), BUILD)


def test_cli_lists_repos_for_the_deploy_script(
    db: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    owner, writer = db
    _registered(owner, ("v-note", "https://x/v.git", "master"), ("old", None, "main"))
    assert cli.main(["list-repos"], {cli.DATABASE_URL_ENV: writer}) == 0
    out = capsys.readouterr().out.splitlines()
    assert "repo v-note https://x/v.git master" in out
    assert "code-vitals list-repos: old has no URL; deployments skip it" in out
    assert not any(line.startswith("repo old") for line in out)


@pytest.fixture
def only_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    monkeypatch.setattr(checks, "registered", lambda: ONLY_FILES)


def _backfill_cli(writer: str, name: str, path: Path) -> int:
    return cli.main(
        ["backfill-repo", "--repo", name, "--url", str(path), "--trunk", "main"],
        {cli.DATABASE_URL_ENV: writer},
    )


@pytest.mark.usefixtures("only_files")
def test_cli_backfill_repo_exits_by_outcome(
    db: tuple[str, str],
    repo: FixtureRepo,
    bare: FixtureRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, writer = db

    assert _backfill_cli(writer, "fixture", repo.path) == 0
    out = capsys.readouterr().out
    assert "fixture: 4 commit(s) measured of 4 to do" in out
    assert "code-vitals backfill-repo: ok" in out

    assert _backfill_cli(writer, "bare", bare.path) == cli.SKIPPED_EXIT == 3
    assert (
        f"bare has no code-vitals plugin step in {WORKFLOW_PATH} at main; skipped"
        in capsys.readouterr().out
    )

    assert _backfill_cli(writer, "gone", repo.path.parent / "nowhere") == 1
    assert "code-vitals backfill-repo: gone failed: git clone of" in capsys.readouterr().err


@pytest.mark.usefixtures("only_files")
def test_cli_backfill_repo_fails_on_a_check_problem(
    db: tuple[str, str],
    repo: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, writer = db
    wrong = checks.load_expected(
        f'[fixture]\nruleset = 2\n\n[fixture.commits.{repo.shas[1]}.files]\nversion = 1\n"-" = 4\n'
    )
    monkeypatch.setattr(checks, "load_expected", lambda: wrong)
    assert _backfill_cli(writer, "fixture", repo.path) == 1
    err = capsys.readouterr().err
    assert "files v1: - expected 4 got 3" in err
    assert "code-vitals backfill-repo: 1 problem(s)" in err


def test_the_migration_fills_the_urls_of_the_repos_measured_before(fresh_db: str) -> None:
    before = [m for m in bundled_migrations() if m.number < 6]
    migrate(fresh_db, migrations=before)
    with psycopg.connect(fresh_db) as conn:
        for name in ("v-note", "bored", "sovereign-config", "other"):
            conn.execute("INSERT INTO repo (name, trunk) VALUES (%s, 'main')", (name,))
    migrate(fresh_db)
    assert _rows(fresh_db, "SELECT name, url FROM repo ORDER BY name") == [
        ("bored", "https://github.com/vcheesbrough/bored.git"),
        ("other", None),
        ("sovereign-config", "https://github.com/vcheesbrough/sovereign-config.git"),
        ("v-note", "https://github.com/vcheesbrough/v-note.git"),
    ]


@pytest.mark.parametrize("url", ["https://user:token@github.com/x/y.git", "a b", ""])
def test_a_url_with_credentials_or_spaces_is_refused(db: tuple[str, str], url: str) -> None:
    owner, _ = db
    with pytest.raises(psycopg.errors.CheckViolation):
        _registered(owner, ("x", url, "main"))
