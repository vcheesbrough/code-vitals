"""The Woodpecker plugin, against a fake docker that records every call and the
environment each one saw."""

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from code_vitals import cli, plugin, settings
from code_vitals.build_info import BuildInfo

from .measured_repos import MEASURED

BUILD = BuildInfo(version="0.13.42", revision="abc123")
PASSWORD = "s3cret/with@odd:chars"
STEP_ENV = {
    "PLUGIN_TRUNK": "master",
    "PLUGIN_VERSION_SOURCES": json.dumps(
        [
            {"path": "Cargo.toml", "kind": "cargo-workspace"},
            {"path": "Cargo.toml", "kind": "cargo-package"},
            {"path": "version.txt", "kind": "plain"},
        ]
    ),
    "PLUGIN_AREAS": json.dumps(
        {
            "version": 1,
            "rules": [
                {"glob": "crates/**", "area": "server"},
                {"glob": "frontend/**", "area": "web"},
                {"glob": "android/**", "area": "android"},
            ],
        }
    ),
    "PLUGIN_WRITER_PASSWORD": PASSWORD,
    "CI_REPO_NAME": "v-note",
    "CI_REPO_CLONE_URL": "https://github.com/vcheesbrough/v-note.git",
    "CI_PIPELINE_NUMBER": "77",
}

FAKE_DOCKER = """#!{python}
import json, os, sys
log = {log!r}
watched = ["CODE_VITALS_DATABASE_URL", "CODE_VITALS_SETTINGS", "OTEL_EXPORTER_OTLP_ENDPOINT",
           "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_RESOURCE_ATTRIBUTES", "CI_PIPELINE_NUMBER",
           "PLUGIN_WRITER_PASSWORD"]
with open(log, "a") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "env": {{k: os.environ.get(k) for k in watched}},
                         "stdin": sys.stdin.read()}}) + "\\n")
fail = os.environ.get("FAKE_DOCKER_FAIL", "")
if sys.argv[1] == fail:
    sys.exit(5)
if sys.argv[1] == "create":
    print("cid123")
if sys.argv[1] == "start":
    print("collector output")
    sys.exit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))
"""


@pytest.fixture
def docker(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.jsonl"
    path = tmp_path / "docker"
    path.write_text(FAKE_DOCKER.format(python=sys.executable, log=str(log)))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path), log


def _calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_starts_this_builds_collector_on_both_networks(
    docker: tuple[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    path, log = docker
    assert plugin.run(STEP_ENV, BUILD, docker=path) == 0

    calls = _calls(log)
    assert [c["args"][:2] for c in calls] == [
        ["create", "--rm"],
        ["network", "connect"],
        ["start", "--attach"],
        ["rm", "-f"],
    ]
    create = calls[0]["args"]
    image = create.index("registry.desync.link/code-vitals-collector:0.13.42")
    assert create[image + 1 :] == [
        "collect",
        "--repo",
        "v-note",
        "--url",
        "https://github.com/vcheesbrough/v-note.git",
        "--fetch",
        "--trigger",
        "push",
    ]
    assert create[2:6] == ["--pull", "missing", "--network", "code-vitals_default"]
    # Every value reaches the container by name only.
    names = [create[i + 1] for i, arg in enumerate(create[:image]) if arg == "-e"]
    assert sorted(names) == sorted(
        [
            "CODE_VITALS_DATABASE_URL",
            "CODE_VITALS_SETTINGS",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "OTEL_RESOURCE_ATTRIBUTES",
            "CI_PIPELINE_NUMBER",
        ]
    )
    assert calls[1]["args"] == ["network", "connect", "proxy-backend", "cid123"]
    assert calls[2]["args"] == ["start", "--attach", "cid123"]
    assert calls[3]["args"] == ["rm", "-f", "cid123"]

    env = calls[0]["env"]
    assert env["CODE_VITALS_DATABASE_URL"] == (
        "postgresql://code_vitals_writer:s3cret%2Fwith%40odd%3Achars@code-vitals-db:5432/code_vitals"
    )
    assert settings.from_json(env["CODE_VITALS_SETTINGS"]) == MEASURED["v-note"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://monitor-alloy:4318"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment.name=prod"
    assert env["CI_PIPELINE_NUMBER"] == "77"
    # The plugin's settings stay out of docker's environment, and the password
    # is on no command line; nothing reads stdin.
    assert all(c["env"]["PLUGIN_WRITER_PASSWORD"] is None for c in calls)
    assert not any(PASSWORD in " ".join(c["args"]) for c in calls)
    assert all(c["stdin"] == "" for c in calls)

    out = capfd.readouterr().out
    assert "v-note from https://github.com/vcheesbrough/v-note.git, trunk master" in out
    assert "collector output" in out


def test_the_collectors_status_is_the_steps(
    docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, log = docker
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "1")
    assert plugin.run(STEP_ENV, BUILD, docker=path) == 1
    assert _calls(log)[-1]["args"] == ["rm", "-f", "cid123"]


@pytest.mark.parametrize("failing", ["create", "network"])
def test_a_docker_failure_fails_the_step_and_leaves_nothing_behind(
    docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path, log = docker
    monkeypatch.setenv("FAKE_DOCKER_FAIL", failing)
    assert plugin.run(STEP_ENV, BUILD, docker=path) == 5
    commands = [c["args"][0] for c in _calls(log)]
    assert "start" not in commands
    if failing == "network":
        assert commands[-1] == "rm"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"PLUGIN_TRUNK": None}, "trunk must be a branch name"),
        ({"PLUGIN_AREAS": '{"version": 1, "exclude": []}'}, "areas has unknown key"),
        ({"PLUGIN_COLOUR": "red"}, r"unknown setting\(s\) colour"),
        ({"PLUGIN_WRITER_PASSWORD": None}, "the writer_password setting is required"),
        ({"PLUGIN_WRITER_PASSWORD": ""}, "the writer_password setting is required"),
        ({"CI_REPO_NAME": None}, "CI_REPO_NAME must be the repo's name"),
        ({"CI_REPO_NAME": "a b"}, "CI_REPO_NAME must be the repo's name"),
        ({"CI_REPO_CLONE_URL": "git@github.com:x/y.git"}, "public http"),
        ({"CI_REPO_CLONE_URL": "https://user:token@github.com/x/y.git"}, "public http"),
    ],
)
def test_a_misconfigured_step_fails_before_docker(
    docker: tuple[str, Path],
    capfd: pytest.CaptureFixture[str],
    change: dict[str, str | None],
    message: str,
) -> None:
    path, log = docker
    merged = {**STEP_ENV, **change}
    environ = {k: v for k, v in merged.items() if v is not None}
    assert plugin.run(environ, BUILD, docker=path) == 2
    err = capfd.readouterr().err
    assert "code-vitals plugin: invalid step:" in err
    assert re.search(message, err)
    assert _calls(log) == []


def test_the_cli_runs_the_plugin_without_a_database(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # No CODE_VITALS_DATABASE_URL: the plugin needs none of its own.
    seen: list[tuple[dict[str, str], BuildInfo]] = []

    def run(environ: dict[str, str], info: BuildInfo) -> int:
        seen.append((environ, info))
        return 0

    monkeypatch.setattr(plugin, "run", run)
    assert cli.main(["plugin"], {"PLUGIN_TRUNK": "main"}) == 0
    assert seen and seen[0][0] == {"PLUGIN_TRUNK": "main"}
    assert "telemetry" not in capfd.readouterr().out


def test_the_collector_image_is_this_builds_version() -> None:
    assert plugin.collector_image(BUILD) == "registry.desync.link/code-vitals-collector:0.13.42"
