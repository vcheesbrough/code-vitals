from pathlib import Path

import pytest

from code_vitals.gitrepo import Clone, GitError

from .gitfixture import make_repo, three_commit_history


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

    assert history == [first, second, merge]
    assert side not in history


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
