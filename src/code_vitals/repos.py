"""The repositories code-vitals measures, and how to read each one.

Changing a repo's entry changes recorded commit attributes (its iteration)
for new commits only; area rules, which change measured values, arrive with
their own versioning (card #495).
"""

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum

from code_vitals.areas import GENERIC, AreaRuleset, ruleset


class VersionFile(StrEnum):
    """Where a repo has kept its version; the minor is the iteration."""

    CARGO_WORKSPACE = "cargo-workspace"  # Cargo.toml [workspace.package].version
    CARGO_PACKAGE = "cargo-package"  # Cargo.toml [package].version
    PYPROJECT = "pyproject"  # pyproject.toml [project].version
    PLAIN = "plain"  # the file is just the version


@dataclass(frozen=True)
class VersionSource:
    path: str
    kind: VersionFile


@dataclass(frozen=True)
class RepoConfig:
    name: str
    trunk: str
    # Tried in order at each commit; the first that yields a version wins, so
    # a repo that moved its version keeps an iteration across the move.
    version_sources: tuple[VersionSource, ...]
    areas: AreaRuleset = GENERIC


CARGO_SOURCES = (
    VersionSource("Cargo.toml", VersionFile.CARGO_WORKSPACE),
    VersionSource("Cargo.toml", VersionFile.CARGO_PACKAGE),
)

REPOS: dict[str, RepoConfig] = {
    # version.txt is older than [workspace.package] in v-note and has gone
    # stale (0.54 against Cargo's 0.69 on 2026-10-21), so Cargo comes first.
    "v-note": RepoConfig(
        "v-note",
        "master",
        (*CARGO_SOURCES, VersionSource("version.txt", VersionFile.PLAIN)),
        ruleset(
            1,
            ("crates/**", "server"),  # the axum server and its protocol crate
            ("frontend/**", "web"),
            ("android/**", "android"),
        ),
    ),
    "bored": RepoConfig(
        "bored",
        "main",
        CARGO_SOURCES,
        ruleset(
            1,
            ("backend/**", "backend"),
            ("frontend/**", "web"),
            ("shared/**", "shared"),
            ("mcp/**", "shared"),
        ),
    ),
    "sovereign-config": RepoConfig(
        "sovereign-config",
        "main",
        CARGO_SOURCES,
        ruleset(
            1,
            ("crates/sovereign-config-server/**", "server"),
            ("crates/sovereign-config-woodpecker-broker/**", "server"),
            ("crates/sovereign-config-web/**", "web"),
            ("crates/**", "libraries"),
            ("proto/**", "libraries"),
        ),
    ),
}

_SEMVER = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


def version_from(kind: VersionFile, text: str) -> str | None:
    """The version string a version file holds, or None."""
    if kind is VersionFile.PLAIN:
        line = text.strip().splitlines()[0] if text.strip() else ""
        return line.strip() or None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if kind is VersionFile.CARGO_WORKSPACE:
        table = data.get("workspace", {}).get("package", {})
    elif kind is VersionFile.CARGO_PACKAGE:
        table = data.get("package", {})
    else:
        table = data.get("project", {})
    version = table.get("version") if isinstance(table, dict) else None
    # `version.workspace = true` in a member is a table, not a version.
    return version if isinstance(version, str) else None


def iteration_of(version: str | None) -> int | None:
    """The minor of a MAJOR.MINOR.PATCH version: the iteration number."""
    if version is None:
        return None
    match = _SEMVER.match(version)
    return int(match[2]) if match else None


_PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$")


def pr_number_of(subject: str) -> int | None:
    """The PR number a squash merge appends to its subject: `… (#123)`."""
    match = _PR_NUMBER.search(subject)
    return int(match[1]) if match else None
