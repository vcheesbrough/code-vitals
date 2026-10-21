import pytest

from code_vitals.repos import REPOS, VersionFile, iteration_of, pr_number_of, version_from

CARGO = """
[workspace]
members = ["shared"]

[package]
name = "member"
version = "9.9.9"

[workspace.package]
version = "1.73.0"
"""


def test_cargo_workspace_and_package_versions_are_read_from_their_own_tables() -> None:
    assert version_from(VersionFile.CARGO_WORKSPACE, CARGO) == "1.73.0"
    assert version_from(VersionFile.CARGO_PACKAGE, CARGO) == "9.9.9"


def test_a_member_inheriting_the_workspace_version_has_no_version_of_its_own() -> None:
    member = '[package]\nname = "m"\nversion.workspace = true\n'
    assert version_from(VersionFile.CARGO_PACKAGE, member) is None


def test_pyproject_and_plain_versions() -> None:
    assert version_from(VersionFile.PYPROJECT, '[project]\nversion = "0.4.0"\n') == "0.4.0"
    assert version_from(VersionFile.PLAIN, "0.54.0\n") == "0.54.0"
    assert version_from(VersionFile.PLAIN, "\n") is None


def test_unparseable_toml_yields_no_version() -> None:
    assert version_from(VersionFile.CARGO_WORKSPACE, "[[[") is None


@pytest.mark.parametrize(
    ("version", "iteration"),
    [("1.73.0", 73), ("0.4.12", 4), ("v2.46.418", 46), ("not a version", None), (None, None)],
)
def test_the_iteration_is_the_minor(version: str | None, iteration: int | None) -> None:
    assert iteration_of(version) == iteration


@pytest.mark.parametrize(
    ("subject", "number"),
    [
        ("Iteration 3 — Superset (#3)", 3),
        ("fix: grant types (#4)  ", 4),
        ("mentions (#5) mid-subject only", None),
        ("no number", None),
    ],
)
def test_pr_number_comes_from_the_squash_merge_suffix(subject: str, number: int | None) -> None:
    assert pr_number_of(subject) == number


def test_the_measured_repos_and_their_trunks() -> None:
    assert {name: repo.trunk for name, repo in REPOS.items()} == {
        "v-note": "master",
        "bored": "main",
        "sovereign-config": "main",
    }
    # v-note's version.txt went stale; Cargo must be tried first.
    assert REPOS["v-note"].version_sources[0].path == "Cargo.toml"
