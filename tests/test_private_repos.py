"""Private repos (card #602): a repo served only with its clone token is
registered, measured and re-measured by deployments, and the token appears in
no log, span, argument, database row or clone.

The repo is served by a local smart-HTTP git server that refuses every request
without the token (tests/git_http_server.py), as GitHub does for a private repo.
"""

import io
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from code_vitals import checks, cli
from code_vitals import collect as collect_module
from code_vitals.backfill import Outcome, RegisteredRepo, backfill_repo, registered_repos
from code_vitals.build_info import BuildInfo
from code_vitals.db.migrate import migrate
from code_vitals.gitrepo import GitError
from code_vitals.measures import Measure
from code_vitals.measures.files import Files
from code_vitals.settings import WORKFLOW_PATH

from .conftest import TEST_PASSWORDS, role_url
from .git_http_server import GitHttpServer
from .gitfixture import FixtureRepo, three_commit_history
from .otlp_receiver import OtlpReceiver

TOKEN = "ghp_PrivateRepoT0kenForTests"
BUILD = BuildInfo(version="0.17.99", revision="abc123")
ONLY_FILES: list[Measure] = [Files()]
RUN = "import sys; from code_vitals import cli; sys.exit(cli.main(sys.argv[1:]))"
SETTINGS = '{"trunk": "main", "version_sources": [{"path": "pyproject.toml", "kind": "pyproject"}]}'
# The private repo's own plugin step, as mini-config would write it.
WORKFLOW = """when:
  - event: push
    branch: main
steps:
  code-vitals:
    image: registry.desync.link/code-vitals-plugin:stable
    failure: ignore
    settings:
      trunk: main
      version_sources: [{path: pyproject.toml, kind: pyproject}]
      writer_password: {from_secret: code_vitals_prod_writer_password}
      clone_token: {from_secret: github_token}
"""


@pytest.fixture
def db(fresh_db: str) -> Iterator[tuple[str, str]]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db, role_url(fresh_db, "code_vitals_writer")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, FixtureRepo, GitHttpServer]]:
    """A private repo with the plugin step at its head: its URL, the source
    repo and the server, which serves it to the token only."""
    (tmp_path / "served").mkdir()
    source = three_commit_history(tmp_path / "served")
    source.shas.append(source.commit("Measure me", {WORKFLOW_PATH: WORKFLOW}))
    with GitHttpServer(tmp_path / "served", TOKEN) as server:
        yield server.url("repo"), source, server


@pytest.fixture
def only_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    monkeypatch.setattr(checks, "registered", lambda: ONLY_FILES)


def _rows(url: str, query: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(query).fetchall()


def _database_text(owner: str) -> str:
    """Every row of every table, as text."""
    with psycopg.connect(owner) as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
        return "\n".join(
            str(row[0])
            for (table,) in tables
            for row in conn.execute(
                sql.SQL("SELECT t::text FROM {} t").format(sql.Identifier(str(table)))
            ).fetchall()
        )


def _collect_cli(
    writer: str, url: str, stdin: str | None, otel: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`collect --fetch` as the plugin starts it, in its own process."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update(
        {"CODE_VITALS_DATABASE_URL": writer, "CODE_VITALS_SETTINGS": SETTINGS, **(otel or {})}
    )
    args = ["collect", "--repo", "private", "--url", url, "--fetch", "--trigger", "push"]
    if stdin is not None:
        args.append(cli.TOKEN_STDIN)
    return subprocess.run(
        [sys.executable, "-c", RUN, *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_private_repo_is_registered_and_measured_with_its_token_leaked_nowhere(
    db: tuple[str, str], served: tuple[str, FixtureRepo, GitHttpServer]
) -> None:
    owner, writer = db
    url, source, _ = served
    with OtlpReceiver() as receiver:
        result = _collect_cli(
            writer,
            url,
            f"{TOKEN}\n",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": receiver.endpoint,
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=test",
            },
        )
    assert result.returncode == 0, result.stderr
    assert "4 commit(s) measured of 4 to do" in result.stdout
    assert _rows(owner, "SELECT name, url, private FROM repo") == [("private", url, True)]
    assert _rows(owner, "SELECT count(*) FROM commit") == [(len(source.shas),)]

    received = receiver.received
    assert list(received.spans()), "telemetry arrived"
    exported = b"".join(m.SerializeToString() for m in received.traces + received.logs)
    exported += b"".join(m.SerializeToString() for m in received.metrics)
    assert TOKEN.encode() not in exported
    assert TOKEN not in result.stdout + result.stderr
    assert TOKEN not in _database_text(owner)


def test_without_its_token_a_private_repo_fails_and_keeps_its_access(
    db: tuple[str, str], served: tuple[str, FixtureRepo, GitHttpServer]
) -> None:
    owner, writer = db
    url, _, _ = served
    assert _collect_cli(writer, url, f"{TOKEN}\n").returncode == 0

    result = _collect_cli(writer, url, None)
    assert result.returncode == 1
    assert f"git clone of {url} failed" in result.stderr
    # A failed clone records nothing about the repo's access.
    assert _rows(owner, "SELECT private FROM repo") == [(True,)]
    assert _rows(owner, "SELECT outcome::text FROM run ORDER BY id DESC LIMIT 1") == [("failure",)]


@pytest.mark.usefixtures("only_files")
def test_a_repo_cloned_without_a_token_is_recorded_public(
    db: tuple[str, str], served: tuple[str, FixtureRepo, GitHttpServer]
) -> None:
    owner, writer = db
    url, source, _ = served
    assert _collect_cli(writer, url, f"{TOKEN}\n").returncode == 0
    # The repo went public (and dropped its clone_token): the next push says so.
    # Its local path stands for the anonymous URL.
    with psycopg.connect(owner) as conn:
        conn.execute("UPDATE repo SET url = %s", (str(source.path),))
    environ = {cli.DATABASE_URL_ENV: writer, "CODE_VITALS_SETTINGS": SETTINGS}
    args = ["collect", "--repo", "private", "--url", str(source.path), "--fetch"]
    assert cli.main(args, environ) == 0
    assert _rows(owner, "SELECT private FROM repo") == [(False,)]


def test_an_empty_token_on_stdin_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {cli.DATABASE_URL_ENV: "postgresql://unused", "CODE_VITALS_SETTINGS": SETTINGS}
    args = ["collect", "--repo", "x", "--url", "https://x/y.git", "--fetch", cli.TOKEN_STDIN]
    assert cli.main(args, environ, io.StringIO("\n")) == 2
    assert "--clone-token-stdin but no token on stdin" in capsys.readouterr().err


def test_deployments_list_a_private_repo_as_private(db: tuple[str, str]) -> None:
    owner, writer = db
    with psycopg.connect(owner) as conn:
        conn.execute(
            "INSERT INTO repo (name, url, trunk, private) VALUES"
            " ('mini-config', 'https://x/m.git', 'master', true),"
            " ('bored', 'https://x/b.git', 'main', false)"
        )
    assert registered_repos(writer) == [
        RegisteredRepo("bored", "https://x/b.git", "main", private=False),
        RegisteredRepo("mini-config", "https://x/m.git", "master", private=True),
    ]


def test_a_private_repo_is_backfilled_with_its_token(
    db: tuple[str, str], served: tuple[str, FixtureRepo, GitHttpServer]
) -> None:
    owner, writer = db
    url, _, _ = served
    registered = RegisteredRepo("private", url, "main", private=True)
    result = backfill_repo(writer, registered, BUILD, measures=ONLY_FILES, clone_token=TOKEN)
    assert result.outcome is Outcome.CHECKED and result.problems == ()
    assert result.collected is not None and result.collected.commits_measured == 4
    # Read from its own workflow file, clone_token and all.
    assert result.settings is not None and result.settings.trunk == "main"
    assert TOKEN not in _database_text(owner)


def test_without_its_token_a_private_repo_fails_its_backfill_and_is_not_skipped(
    db: tuple[str, str], served: tuple[str, FixtureRepo, GitHttpServer]
) -> None:
    owner, writer = db
    url, _, _ = served
    with psycopg.connect(owner) as conn:
        conn.execute(
            "INSERT INTO repo (name, url, trunk, private) VALUES ('private', %s, 'main', true)",
            (url,),
        )
    with pytest.raises(GitError, match=r"git clone of .* failed"):
        backfill_repo(writer, RegisteredRepo("private", url, "main", True), BUILD)
    assert _rows(owner, "SELECT skipped_at FROM repo") == [(None,)]


@pytest.mark.usefixtures("only_files")
def test_the_cli_backfills_a_private_repo_with_the_token_on_stdin(
    db: tuple[str, str],
    served: tuple[str, FixtureRepo, GitHttpServer],
    capfd: pytest.CaptureFixture[str],
) -> None:
    _, writer = db
    url, _, _ = served
    args = ["backfill-repo", "--repo", "private", "--url", url, "--trunk", "main"]
    environ = {cli.DATABASE_URL_ENV: writer}

    assert cli.main([*args, cli.TOKEN_STDIN], environ, io.StringIO(f"{TOKEN}\n")) == 0
    assert cli.main(args, environ) == 1  # failed, not skipped (3)
    captured = capfd.readouterr()
    assert "code-vitals backfill-repo: ok" in captured.out
    assert f"private failed: git clone of {url} failed" in captured.err
    assert TOKEN not in captured.out + captured.err
