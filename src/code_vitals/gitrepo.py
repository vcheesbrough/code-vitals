"""Read-only access to a clone of a measured repository, through `git`.

Nothing here writes to the clone (no worktrees, no checkouts), so it can be
mounted read-only. Every git call runs in its own span.
"""

import subprocess
import tarfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from code_vitals import obs


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    committed_at: datetime
    author: str
    subject: str


@dataclass(frozen=True)
class ExtractedTree:
    path: Path
    files: int
    skipped: int  # members refused as unsafe (absolute paths, links escaping the tree …)


class Clone:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, command: str, *args: str) -> str:
        with obs.git_span(command):
            result = subprocess.run(
                ["git", "-C", str(self.path), command, *args],
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            raise GitError(f"git {command} failed: {result.stderr.strip()}")
        return result.stdout

    def resolve_trunk(self, trunk: str) -> str:
        """The trunk's head sha: the remote-tracking branch if the clone has one
        (a CI clone checks out a detached commit), else the local branch."""
        for ref in (f"refs/remotes/origin/{trunk}", f"refs/heads/{trunk}"):
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.path),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    ref + "^{commit}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        raise GitError(f"no trunk {trunk!r} in {self.path} (neither origin/{trunk} nor {trunk})")

    def first_parent_history(self, head: str) -> list[str]:
        """Trunk commits, oldest first, following first parents only: a squash
        or merge commit is one step, and merged branches are not walked."""
        return self._git("rev-list", "--first-parent", "--reverse", head).split()

    def commit_info(self, sha: str) -> CommitInfo:
        out = self._git("show", "-s", "--format=%H%x00%cI%x00%an%x00%s", sha)
        full_sha, committed_at, author, subject = out.rstrip("\n").split("\x00", 3)
        return CommitInfo(full_sha, datetime.fromisoformat(committed_at), author, subject)

    def tags_by_commit(self) -> dict[str, list[str]]:
        """Every tag, by the commit it points at (annotated tags peeled)."""
        out = self._git(
            "for-each-ref",
            "refs/tags",
            "--format=%(objectname)%00%(*objectname)%00%(refname:short)",
        )
        tags: dict[str, list[str]] = defaultdict(list)
        for line in out.splitlines():
            obj, peeled, name = line.split("\x00")
            tags[peeled or obj].append(name)
        return {sha: sorted(names) for sha, names in tags.items()}

    def read_file(self, sha: str, path: str) -> str | None:
        """A file's content at a commit, or None if it did not exist there."""
        result = subprocess.run(
            ["git", "-C", str(self.path), "show", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def extract_tree(self, sha: str, dest: Path) -> ExtractedTree:
        """The commit's tree as files under `dest`, via `git archive` streamed
        into tarfile with the `data` filter; members it refuses are skipped."""
        files = 0
        skipped = 0

        def safe(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
            nonlocal files, skipped
            try:
                member = tarfile.data_filter(member, path)
            except tarfile.FilterError:
                skipped += 1
                return None
            if member.isfile():
                files += 1
            return member

        with obs.git_span("archive"):
            proc = subprocess.Popen(
                ["git", "-C", str(self.path), "archive", "--format=tar", sha],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
                archive.extractall(dest, filter=safe)
            _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise GitError(f"git archive {sha} failed: {stderr.decode(errors='replace').strip()}")
        return ExtractedTree(dest, files, skipped)
