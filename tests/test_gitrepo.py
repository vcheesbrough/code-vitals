import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from code_vitals.gitrepo import TOKEN_ENV, Clone, GitError, clone_from

from .git_http_server import GitHttpServer
from .gitfixture import FixtureRepo, make_repo, three_commit_history


def test_history_is_first_parent_oldest_first(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = repo.commit("one", {"a": "1"})
    repo.git("switch", "-q", "-c", "feature")
    side = repo.commit("side", {"b": "2"})
    repo.git("switch", "-q", "main")
    second = repo.commit("two", {"c": "3"})
    repo.git("merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge = repo.git("rev-parse", "HEAD")

    history = Clone(repo.path).first_parent_history(merge)

    assert history == [(first, None), (second, first), (merge, second)]
    assert side not in {sha for sha, _ in history}


def test_trunk_prefers_the_remote_tracking_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    old = repo.commit("old", {"a": "1"})
    new = repo.commit("new", {"a": "2"})
    repo.git("update-ref", "refs/remotes/origin/main", old)

    assert Clone(repo.path).resolve_trunk("main") == old
    repo.git("update-ref", "-d", "refs/remotes/origin/main")
    assert Clone(repo.path).resolve_trunk("main") == new
    with pytest.raises(GitError, match="no trunk"):
        Clone(repo.path).resolve_trunk("master")


def test_commit_info_and_tags(tmp_path: Path) -> None:
    repo = three_commit_history(tmp_path)
    repo.git("tag", "lightweight", repo.shas[0])
    clone = Clone(repo.path)

    info = clone.commit_info(repo.shas[1])
    assert info.sha == repo.shas[1]
    assert info.subject == "Iteration 2 — add things (#7)"
    assert info.author == "Fixture Author"
    assert info.committed_at.tzinfo is not None

    tags = clone.tags_by_commit()
    assert tags[repo.shas[1]] == ["0.2.9"]  # annotated tag, peeled to its commit
    assert tags[repo.shas[0]] == ["lightweight"]


def test_read_file_at_a_commit(tmp_path: Path) -> None:
    repo = three_commit_history(tmp_path)
    clone = Clone(repo.path)
    assert clone.read_file(repo.shas[1], "a.py") == "a = 1\n"
    assert clone.read_file(repo.shas[2], "a.py") is None


def test_extract_tree_counts_files_and_skips_links_that_escape(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo.path / "inside").write_text("x")
    (repo.path / "ok-link").symlink_to("inside")
    (repo.path / "escape").symlink_to("/etc/passwd")
    repo.git("add", "inside", "ok-link", "escape")
    sha = repo.commit("links", {"dir/f.txt": "y"})
    dest = tmp_path / "out"
    dest.mkdir()

    tree = Clone(repo.path).extract_tree(sha, dest)

    assert tree.files == 2  # inside, dir/f.txt
    assert tree.skipped == 1  # the absolute link
    assert (dest / "dir" / "f.txt").read_text() == "y"
    assert not (dest / "escape").exists()


def test_extract_tree_never_writes_to_the_clone(tmp_path: Path) -> None:
    repo = three_commit_history(tmp_path)
    before = sorted(p.name for p in (repo.path / ".git").iterdir())
    dest = tmp_path / "out"
    dest.mkdir()
    Clone(repo.path).extract_tree(repo.shas[1], dest)
    assert sorted(p.name for p in (repo.path / ".git").iterdir()) == before


def test_extract_tree_ignores_export_attributes(tmp_path: Path) -> None:
    # git archive would drop export-ignore files; measured values must not
    # depend on a repo's export settings.
    repo = make_repo(tmp_path)
    sha = repo.commit(
        "attrs",
        {".gitattributes": "hidden.txt export-ignore\n", "hidden.txt": "h", "shown.txt": "s"},
    )
    dest = tmp_path / "out"
    dest.mkdir()

    tree = Clone(repo.path).extract_tree(sha, dest)

    assert tree.files == 3
    assert (dest / "hidden.txt").read_text() == "h"


def test_clone_from_is_a_full_clone_without_a_working_tree(tmp_path: Path) -> None:
    source = three_commit_history(tmp_path)
    clone = clone_from(str(source.path), tmp_path / "fetched")
    assert clone.resolve_trunk("main") == source.shas[-1]
    assert [sha for sha, _ in clone.first_parent_history(source.shas[-1])] == source.shas
    assert clone.tags_by_commit() == {source.shas[1]: ["0.2.9"]}
    assert not (tmp_path / "fetched" / "pyproject.toml").exists()


def test_clone_from_never_waits_on_a_credential_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 128, "", "terminal prompts disabled")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GitError, match="terminal prompts disabled"):
        clone_from("https://example.invalid/private.git", tmp_path / "x")
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


# --- private repos (card #602) ---------------------------------------------------

TOKEN = "ghp_t0kenForTheTest"


@pytest.fixture
def private(tmp_path: Path) -> Iterator[tuple[GitHttpServer, str, FixtureRepo]]:
    """The three-commit history, served only to requests carrying TOKEN."""
    (tmp_path / "served").mkdir()
    source = three_commit_history(tmp_path / "served")
    with GitHttpServer(tmp_path / "served", TOKEN) as server:
        yield server, server.url("repo"), source


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME whose git config stores every credential git approves: what a
    clone must not let happen to its token."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    return home


def _files_containing(root: Path, text: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and text.encode() in p.read_bytes()]


def test_a_private_repo_is_cloned_with_its_token(
    tmp_path: Path, private: tuple[GitHttpServer, str, FixtureRepo], isolated_home: Path
) -> None:
    server, url, source = private
    clone = clone_from(url, tmp_path / "fetched", TOKEN)

    assert [sha for sha, _ in clone.first_parent_history(source.shas[-1])] == source.shas
    assert any(a.startswith("Basic ") for a in server.authorizations)
    # Nothing can read the token back: not the clone's config (its remote is
    # the plain URL), not any file in it, not a credential store.
    config = (tmp_path / "fetched" / ".git" / "config").read_text()
    assert f"url = {url}\n" in config
    assert "credential" not in config
    assert _files_containing(tmp_path / "fetched", TOKEN) == []
    assert not (isolated_home / ".git-credentials").exists()
    assert _files_containing(isolated_home, TOKEN) == []


def test_a_private_repo_without_its_token_fails_without_a_prompt(
    tmp_path: Path, private: tuple[GitHttpServer, str, FixtureRepo], isolated_home: Path
) -> None:
    server, url, _ = private
    with pytest.raises(GitError, match=f"git clone of {url} failed"):
        clone_from(url, tmp_path / "fetched")
    assert server.authorizations and set(server.authorizations) == {""}


def test_a_wrong_token_fails_and_is_not_reported(
    tmp_path: Path, private: tuple[GitHttpServer, str, FixtureRepo], isolated_home: Path
) -> None:
    _, url, _ = private
    wrong = "ghp_wr0ngTokenValue"
    with pytest.raises(GitError) as raised:
        clone_from(url, tmp_path / "fetched", wrong)
    assert wrong not in str(raised.value)
    assert _files_containing(isolated_home, wrong) == []


def test_the_token_is_on_no_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 128, "", f"denied for {TOKEN}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GitError) as raised:
        clone_from("https://example.invalid/private.git", tmp_path / "x", TOKEN)
    [(args, env)] = seen
    assert not any(TOKEN in arg for arg in args)
    assert env[TOKEN_ENV] == TOKEN
    # Even if git ever printed it, the error would not carry it.
    assert TOKEN not in str(raised.value)


def test_a_public_clone_gets_no_token_and_no_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv(TOKEN_ENV, "leftover")
    monkeypatch.setattr(subprocess, "run", fake_run)
    clone_from("https://example.invalid/public.git", tmp_path / "x")
    [(args, env)] = seen
    assert args[:2] == ["git", "clone"]
    assert TOKEN_ENV not in env
