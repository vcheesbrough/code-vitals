"""Read-only access to a clone of a measured repository, through `git`.

Nothing here writes to the clone (no worktrees, no checkouts), so it can be
mounted read-only. Every git call runs in its own span.
"""

import subprocess
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


# git's well-known empty tree: the "parent" of a root commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int  # 0 for a binary file
    deleted: int
    binary: bool


@dataclass(frozen=True)
class ExtractedTree:
    path: Path
    files: int
    skipped: int  # entries not written: submodules, unsafe paths, links leaving the tree


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

    def first_parent_history(self, head: str) -> list[tuple[str, str | None]]:
        """Trunk commits, oldest first, with each one's first parent (None for
        a root commit), following first parents only: a squash or merge commit
        is one step, and merged branches are not walked. Refuses a shallow
        clone, whose oldest commit would look like a root and record the whole
        tree as added lines."""
        if self._git("rev-parse", "--is-shallow-repository").strip() == "true":
            raise GitError(f"{self.path} is a shallow clone; the collector needs full history")
        history = []
        for line in self._git(
            "rev-list", "--first-parent", "--parents", "--reverse", head
        ).splitlines():
            sha, *parents = line.split()
            history.append((sha, parents[0] if parents else None))
        return history

    def commit_info(self, sha: str) -> CommitInfo:
        out = self._git("show", "-s", "--format=%H%x00%cI%x00%an%x00%s", sha)
        full_sha, committed_at, author, subject = out.rstrip("\n").split("\x00", 3)
        return CommitInfo(full_sha, datetime.fromisoformat(committed_at), author, subject)

    def commit_times(self, head: str) -> dict[str, datetime]:
        """Each first-parent commit's commit time, in one git call."""
        out = self._git("log", "--first-parent", "--format=%H%x00%cI", head)
        times = {}
        for line in out.splitlines():
            sha, committed_at = line.split("\x00")
            times[sha] = datetime.fromisoformat(committed_at)
        return times

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

    def changes(self, parent: str | None, sha: str) -> list[FileChange]:
        """Per-file line changes from `parent` (the empty tree for a root
        commit) to `sha`, from git's object data alone.

        `git diff --numstat` applies the `diff`/`binary` attributes of whatever
        `.gitattributes` the clone has checked out — not the measured commit's —
        so a file marked `-diff` would count no lines. Instead: the changed
        paths come from `diff --raw` (no attributes); a modified file's lines
        from diffing its two blobs by object id (no path, so no attribute can
        match); an added or deleted file's lines from the blob itself. Binary
        files are detected by content (a NUL in the first 8000 bytes, git's own
        rule) and count no lines. Renames are a delete plus an add: rename
        detection is a similarity heuristic that may differ between git
        versions, and recorded values must not. Submodules are skipped."""
        raw = self._git(
            "diff", "--raw", "-z", "--no-renames", "--no-abbrev", parent or EMPTY_TREE, sha
        )
        fields = raw.split("\0")
        entries = []
        for meta, path in zip(fields[0::2], fields[1::2], strict=False):
            if not meta:
                continue
            old_mode, new_mode, old_blob, new_blob, status = meta.lstrip(":").split(" ")
            if "160000" in (old_mode, new_mode):
                continue  # a submodule pointer, not file content
            entries.append((status, old_blob, new_blob, path))

        whole = [b for status, old, new, _ in entries for b in (
            [new] if status == "A" else [old] if status == "D" else []
        )]  # fmt: skip
        blobs = self._blobs(whole)
        changes = []
        for status, old_blob, new_blob, path in entries:
            if status == "A":
                added = _lines(blobs[new_blob])
                changes.append(FileChange(path, max(added, 0), 0, added < 0))
            elif status == "D":
                deleted = _lines(blobs[old_blob])
                changes.append(FileChange(path, 0, max(deleted, 0), deleted < 0))
            else:
                out = self._git("diff", "--numstat", "--no-ext-diff", old_blob, new_blob)
                if not out.strip():
                    changes.append(FileChange(path, 0, 0, False))  # mode change only
                    continue
                a, d = out.split("\t", 2)[:2]
                binary = a == "-"
                changes.append(
                    FileChange(path, 0 if binary else int(a), 0 if binary else int(d), binary)
                )
        return changes

    def _blobs(self, objects: list[str]) -> dict[str, bytes]:
        if not objects:
            return {}
        with obs.git_span("cat-file"):
            result = subprocess.run(
                ["git", "-C", str(self.path), "cat-file", "--batch"],
                input="".join(f"{o}\n" for o in objects).encode(),
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise GitError(f"git cat-file failed: {result.stderr.decode(errors='replace')}")
        blobs = {}
        data = result.stdout
        pos = 0
        for obj in objects:
            end = data.index(b"\n", pos)
            header = data[pos:end].split()
            size = int(header[2])
            blobs[obj] = data[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
        return blobs

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
        """The commit's tree as files under `dest`, exactly as recorded in git.

        Read with `ls-tree` and `cat-file --batch` rather than `git archive`,
        which applies the measured repo's `.gitattributes` (`export-ignore`,
        `export-subst`) and would make measured values depend on export
        settings. Symlinks are recreated only when they stay inside the tree;
        others, and submodules, are skipped (counted)."""
        with obs.git_span("ls-tree"):
            listing = subprocess.run(
                ["git", "-C", str(self.path), "ls-tree", "-r", "-z", "--full-tree", sha],
                capture_output=True,
                check=False,
            )
        if listing.returncode != 0:
            raise GitError(f"git ls-tree {sha} failed: {listing.stderr.decode(errors='replace')}")
        entries = []
        skipped = 0
        for record in listing.stdout.split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, kind, obj = meta.decode().split(" ")
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if kind != "blob" or not _inside(dest, path):
                skipped += 1  # submodules (commit), unsafe paths
                continue
            entries.append((mode, obj, path))

        files = 0
        with obs.git_span("cat-file"):
            proc = subprocess.Popen(
                ["git", "-C", str(self.path), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write("".join(f"{obj}\n" for _, obj, _ in entries).encode())
            proc.stdin.close()
            for mode, obj, path in entries:
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[0].decode() != obj:
                    raise GitError(f"git cat-file: unexpected answer for {obj}: {header!r}")
                content = proc.stdout.read(int(header[2]))
                proc.stdout.read(1)  # the newline after each object
                target = dest / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    link = content.decode("utf-8", errors="surrogateescape")
                    if Path(link).is_absolute() or not _inside(dest, str(Path(path).parent / link)):
                        skipped += 1
                        continue
                    target.symlink_to(link)
                    continue
                target.write_bytes(content)
                if mode == "100755":
                    target.chmod(0o755)
                files += 1
            _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise GitError(f"git cat-file failed: {stderr.decode(errors='replace').strip()}")
        return ExtractedTree(dest, files, skipped)


def _inside(root: Path, relative: str) -> bool:
    """Whether `relative`, resolved lexically under `root`, stays inside it."""
    import os

    joined = os.path.normpath(os.path.join(root, relative))
    return joined == str(root) or joined.startswith(str(root) + os.sep)


def _lines(content: bytes) -> int:
    """A blob's lines as git's numstat counts them, or -1 if it is binary."""
    if b"\0" in content[:8000]:
        return -1
    lines = content.count(b"\n")
    if content and not content.endswith(b"\n"):
        lines += 1
    return lines
