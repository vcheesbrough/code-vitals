"""The one validator of a repo's plugin settings, from each place they arrive."""

import json
from typing import Any

import pytest
import yaml

from code_vitals import settings
from code_vitals.areas import GENERIC, TOOLING, ruleset
from code_vitals.repos import VersionFile, VersionSource
from code_vitals.settings import SettingsError

from .measured_repos import MEASURED, SETTINGS_YAML, workflow

CARD_EXAMPLE = """
when: [{event: [push, manual], branch: master}]
steps:
  code-vitals:
    image: registry.desync.link/code-vitals-plugin:stable
    failure: ignore
    volumes: [/var/run/docker.sock:/var/run/docker.sock]
    settings:
      trunk: master
      version_sources: [{path: Cargo.toml, kind: cargo-workspace}]
      areas:
        version: 1
        rules: [{glob: "crates/**", area: server}, {glob: "frontend/**", area: web}]
      writer_password: {from_secret: code_vitals_prod_writer_password}
"""


def test_the_documented_step_parses() -> None:
    found = settings.from_workflow(CARD_EXAMPLE)
    assert found is not None
    assert found.trunk == "master"
    assert found.version_sources == (VersionSource("Cargo.toml", VersionFile.CARGO_WORKSPACE),)
    assert found.areas == ruleset(1, ("crates/**", "server"), ("frontend/**", "web"))
    repo = found.repo("v-note", "https://github.com/vcheesbrough/v-note.git")
    assert (repo.name, repo.trunk, repo.url) == (
        "v-note",
        "master",
        "https://github.com/vcheesbrough/v-note.git",
    )


def test_only_trunk_is_required() -> None:
    found = settings.parse({"trunk": "main"})
    assert (found.version_sources, found.areas) == ((), GENERIC)


@pytest.mark.parametrize("repo", sorted(SETTINGS_YAML))
def test_json_round_trips_exactly(repo: str) -> None:
    # The plugin hands the collector the settings as JSON; nothing may change
    # on the way, or the collector would record another ruleset.
    found = MEASURED[repo]
    assert settings.from_json(found.as_json()) == found


@pytest.mark.parametrize("repo", sorted(SETTINGS_YAML))
def test_the_plugin_env_reads_as_woodpecker_encodes_it(repo: str) -> None:
    # Woodpecker passes each setting as PLUGIN_<KEY>: strings as they are,
    # maps and lists of maps as JSON.
    raw: dict[str, Any] = yaml.safe_load(SETTINGS_YAML[repo])
    environ = {
        "PLUGIN_TRUNK": raw["trunk"],
        "PLUGIN_VERSION_SOURCES": json.dumps(raw["version_sources"]),
        "PLUGIN_AREAS": json.dumps(raw["areas"]),
        "PLUGIN_WRITER_PASSWORD": "secret",
        "CI_REPO_NAME": repo,
        "HOME": "/root",
    }
    assert settings.from_plugin_env(environ) == MEASURED[repo]


@pytest.mark.parametrize("repo", sorted(SETTINGS_YAML))
def test_the_workflow_file_reads_the_same_settings(repo: str) -> None:
    assert settings.from_workflow(workflow(repo)) == MEASURED[repo]


def test_a_list_of_steps_is_read_too() -> None:
    text = """
steps:
  - name: build
    image: alpine
  - name: measure
    image: registry.desync.link/code-vitals-plugin@sha256:abc
    settings: {trunk: main}
"""
    found = settings.from_workflow(text)
    assert found is not None and found.trunk == "main"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "steps: {}",
        "steps:\n  build:\n    image: alpine\n",
        # The old shell template: the collector image, not the plugin.
        "steps:\n  code-vitals:\n    image: registry.desync.link/code-vitals-collector:stable\n",
        "steps:\n  x:\n    image: registry.desync.link/code-vitals-plugin-other:1\n",
        "just a string",
    ],
)
def test_a_workflow_without_the_plugin_step_has_no_settings(text: str) -> None:
    assert settings.from_workflow(text) is None


def test_two_plugin_steps_are_refused() -> None:
    step = (
        "    image: registry.desync.link/code-vitals-plugin:stable\n    settings: {trunk: main}\n"
    )
    with pytest.raises(SettingsError, match="2 code-vitals plugin steps"):
        settings.from_workflow(f"steps:\n  a:\n{step}  b:\n{step}")


def test_invalid_yaml_is_refused() -> None:
    with pytest.raises(SettingsError, match="not valid YAML"):
        settings.from_workflow("steps: [unclosed")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({}, "trunk must be a branch name, not None"),
        ({"trunk": ""}, "trunk must be a branch name"),
        ({"trunk": "-x"}, "trunk must be a branch name"),
        ({"trunk": "a..b"}, "trunk must be a branch name"),
        ({"trunk": 7}, "trunk must be a branch name"),
        ({"trunk": "main", "colour": "red"}, r"unknown setting\(s\) colour"),
        ({"trunk": "main", "version_sources": "Cargo.toml"}, "version_sources must be a list"),
        (
            {"trunk": "main", "version_sources": [{"path": "Cargo.toml", "kind": "npm"}]},
            r"version_sources\[0\]\.kind must be one of cargo-workspace",
        ),
        (
            {"trunk": "main", "version_sources": [{"path": "/etc/passwd", "kind": "plain"}]},
            r"version_sources\[0\]\.path must be a path relative",
        ),
        (
            {"trunk": "main", "version_sources": [{"path": "../x", "kind": "plain"}]},
            r"version_sources\[0\]\.path must be a path relative",
        ),
        ({"trunk": "main", "version_sources": [{"kind": "plain"}]}, r"\[0\] needs path"),
        ({"trunk": "main", "version_sources": ["Cargo.toml"]}, r"\[0\] must be a map, not str"),
        ({"trunk": "main", "areas": {"rules": []}}, "areas needs version"),
        ({"trunk": "main", "areas": {"version": 0}}, "areas.version must be a whole number"),
        ({"trunk": "main", "areas": {"version": True}}, "areas.version must be a whole number"),
        ({"trunk": "main", "areas": {"version": 1, "exclude": []}}, "unknown key"),
        ({"trunk": "main", "areas": {"version": 1, "rules": {}}}, "areas.rules must be a list"),
        (
            {"trunk": "main", "areas": {"version": 1, "rules": [{"glob": "a/**", "area": "Web"}]}},
            r"areas\.rules\[0\]\.area must be a lower-case name",
        ),
        (
            {"trunk": "main", "areas": {"version": 1, "rules": [{"glob": "a/**", "area": "-"}]}},
            r"areas\.rules\[0\]\.area must be a lower-case name",
        ),
        (
            {"trunk": "main", "areas": {"version": 1, "rules": [{"glob": "a/**", "area": "a/b"}]}},
            r"areas\.rules\[0\]\.area must be a lower-case name",
        ),
        (
            {"trunk": "main", "areas": {"version": 1, "rules": [{"glob": "", "area": "web"}]}},
            r"areas\.rules\[0\]\.glob must be a path relative",
        ),
        ("main", "settings must be a map"),
    ],
)
def test_invalid_settings_are_refused_with_a_message_naming_the_setting(
    value: Any, message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        settings.parse(value)


def test_a_plugin_setting_that_is_not_json_is_refused() -> None:
    with pytest.raises(SettingsError, match="areas is not valid"):
        settings.from_plugin_env({"PLUGIN_TRUNK": "main", "PLUGIN_AREAS": "version=1"})


def test_an_unknown_plugin_setting_is_refused() -> None:
    with pytest.raises(SettingsError, match=r"unknown setting\(s\) version_source"):
        settings.from_plugin_env({"PLUGIN_TRUNK": "main", "PLUGIN_VERSION_SOURCE": "[]"})


def test_area_rules_keep_the_shared_rules_around_them() -> None:
    found = settings.parse(
        {"trunk": "main", "areas": {"version": 2, "rules": [{"glob": "app/**", "area": "app"}]}}
    )
    assert found.areas.version == 2
    assert found.areas.area_of("app/src/main.py") == "app"
    assert found.areas.area_of("app/tests/test_x.py") == "tests"
    assert found.areas.area_of("app/node_modules/x.js") is None
    assert found.areas.area_of("Makefile") == TOOLING
