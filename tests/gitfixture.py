"""Build small real git repositories for tests."""

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FixtureRepo:
    path: Path
    trunk: str = "main"
    shas: list[str] = field(default_factory=list)
    _clock: int = 0

    def git(self, *args: str) -> str:
        self._clock += 1
        stamp = f"2026-10-{1 + self._clock // 24:02d}T{self._clock % 24:02d}:00:00+00:00"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Fixture Author",
            "GIT_COMMITTER_EMAIL": "author@example.com",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, subject: str, files: Mapping[str, str | None]) -> str:
        """Write (or, for None, delete) files and commit them on the current branch."""
        for name, content in files.items():
            target = self.path / name
            if content is None:
                self.git("rm", "-q", name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            self.git("add", name)
        self.git("commit", "-q", "--allow-empty", "-m", subject)
        sha = self.git("rev-parse", "HEAD")
        return sha


def make_repo(tmp_path: Path, trunk: str = "main") -> FixtureRepo:
    path = tmp_path / "repo"
    path.mkdir()
    repo = FixtureRepo(path, trunk)
    repo.git("init", "-q", "-b", trunk)
    return repo


def pyproject(version: str) -> str:
    return f'[project]\nname = "fixture"\nversion = "{version}"\n'


def three_commit_history(tmp_path: Path) -> FixtureRepo:
    """main: 1 file (v0.1) → 3 files (v0.2, PR #7, tagged 0.2.9) → 2 files (v0.3)."""
    repo = make_repo(tmp_path)
    repo.shas.append(repo.commit("Initial", {"pyproject.toml": pyproject("0.1.0")}))
    repo.shas.append(
        repo.commit(
            "Iteration 2 — add things (#7)",
            {"pyproject.toml": pyproject("0.2.0"), "a.py": "a = 1\n", "b/c.py": "c = 2\n"},
        )
    )
    repo.git("tag", "-a", "0.2.9", "-m", "deployed")
    repo.shas.append(repo.commit("Remove a", {"pyproject.toml": pyproject("0.3.0"), "a.py": None}))
    return repo
