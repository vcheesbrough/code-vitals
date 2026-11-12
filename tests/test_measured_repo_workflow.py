"""The workflow each measured repo copies (deploy/measured-repo-workflow.yml).

Rendered for every configured repo, its structure is pinned (trunk pushes
only, never blocking the build, the collector by name through the socket) and
its shell is run against a fake `docker` that records every call.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from code_vitals.repos import REPOS

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "measured-repo-workflow.yml"
STABLE = "registry.desync.link/code-vitals-collector:stable"


def render(repo: str) -> str:
    return TEMPLATE.read_text().replace("__REPO__", repo).replace("__TRUNK__", REPOS[repo].trunk)


def workflow(repo: str) -> dict[str, Any]:
    data = yaml.safe_load(render(repo))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_rendering_fills_every_placeholder(repo: str) -> None:
    text = TEMPLATE.read_text()
    assert "__REPO__" in text and "__TRUNK__" in text
    rendered = render(repo)
    assert "__REPO__" not in rendered and "__TRUNK__" not in rendered
    assert f"collect --repo {repo} --fetch" in rendered


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_it_runs_on_trunk_pushes_only_and_never_blocks_the_build(repo: str) -> None:
    data = workflow(repo)
    assert data["when"] == [{"event": ["push", "manual"], "branch": REPOS[repo].trunk}]
    assert data["skip_clone"] is True
    assert "depends_on" not in data  # no other workflow waits on it
    (step,) = data["steps"].values()
    assert step["failure"] == "ignore"
    assert "@sha256:" in step["image"]
    assert step["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_every_configured_repo_can_be_fetched(repo: str) -> None:
    assert REPOS[repo].url == f"https://github.com/vcheesbrough/{repo}.git"


def test_the_step_never_takes_the_shells_stdin() -> None:
    # Woodpecker pipes the script into the shell: an `exec <…` drops whatever
    # the shell has not yet read, and the step exits 0 having done nothing
    # (v-note pipeline 520). Where the cut falls depends on the shell's read
    # buffer: _run_step's bash catches it (it reads a pipe a byte at a time),
    # dash (CI's sh) and busybox ash may not. This check is shell-independent;
    # keep both, and keep _run_step on bash.
    (step,) = workflow("v-note")["steps"].values()
    for line in step["commands"][0].splitlines():
        assert not TAKES_STDIN.search(line.split("#", 1)[0]), line


# `exec <…` as any command on a line, not only the first (v-note's
# scripts/test-code-vitals-workflow.sh uses the same pattern).
TAKES_STDIN = re.compile(r"(?:^|[;&|(])\s*exec\b.*<")


@pytest.mark.parametrize(
    ("line", "takes"),
    [
        ("exec < /dev/null", True),
        ("  set -eu; exec </dev/null", True),
        ("true && exec 0< /dev/null", True),
        ("exec > log", False),
        ("# exec < /dev/null", False),
        ('docker start --attach "$cid"', False),
    ],
)
def test_the_stdin_pattern(line: str, takes: bool) -> None:
    assert bool(TAKES_STDIN.search(line.split("#", 1)[0])) is takes


def test_the_moving_tag_appears_only_in_the_collector_command() -> None:
    (step,) = workflow("v-note")["steps"].values()
    assert ":stable" not in step["image"]
    assert STABLE in step["commands"][0]


def _run_step(tmp_path: Path, repo: str, *, start_exit: int = 0) -> tuple[int, list[str], str]:
    """Run the step's script as Woodpecker would ($$ unescaped) with a fake
    docker; returns (status, docker calls, environment seen by `create`)."""
    (step,) = workflow(repo)["steps"].values()
    script = step["commands"][0].replace("$$", "$")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls, created_env = tmp_path / "calls", tmp_path / "create-env"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {calls}\n'
        'if [ "$1" = create ]; then\n'
        f'  echo "url=$CODE_VITALS_DATABASE_URL attrs=$OTEL_RESOURCE_ATTRIBUTES'
        f' pipeline=$CI_PIPELINE_NUMBER" > {created_env}\n'
        "  echo cid42\n"
        "fi\n"
        f'if [ "$1" = start ]; then exit {start_exit}; fi\n'
        'if [ "$1" = login ]; then cat > /dev/null; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REGISTRY_USER": "ci",
        "REGISTRY_PASSWORD": "zotpw",
        "CODE_VITALS_WRITER_PASSWORD": "writerpw0123",
        "CI_PIPELINE_NUMBER": "77",
    }
    # As Woodpecker runs it: the script on the shell's stdin. bash, unlike
    # dash, reads a pipe a byte at a time, so a command that takes the
    # shell's stdin cuts the script short right there, whatever its length.
    status = subprocess.run(
        ["bash"], input=script.encode(), env=env, capture_output=True
    ).returncode
    return status, calls.read_text().splitlines(), created_env.read_text().strip()


def test_the_step_runs_the_collector_by_name_on_both_networks(tmp_path: Path) -> None:
    status, calls, created_env = _run_step(tmp_path, "v-note")

    assert status == 0
    assert calls[0] == "login registry.desync.link -u ci --password-stdin"
    assert calls[1] == (
        "create --rm --pull always --network code-vitals_default -e CODE_VITALS_DATABASE_URL"
        " -e OTEL_EXPORTER_OTLP_ENDPOINT -e OTEL_EXPORTER_OTLP_PROTOCOL"
        f" -e OTEL_RESOURCE_ATTRIBUTES -e CI_PIPELINE_NUMBER {STABLE}"
        " collect --repo v-note --fetch --trigger push"
    )
    assert calls[2:] == [
        "network connect proxy-backend cid42",
        "start --attach cid42",
        "rm -f cid42",
    ]
    assert created_env == (
        "url=postgresql://code_vitals_writer:writerpw0123@code-vitals-db:5432/code_vitals"
        " attrs=deployment.environment.name=prod pipeline=77"
    )
    assert not any("writerpw0123" in c or "zotpw" in c for c in calls)


def test_a_failing_collector_fails_the_step_and_is_still_removed(tmp_path: Path) -> None:
    # The step fails (and `failure: ignore` keeps the pipeline green).
    status, calls, _ = _run_step(tmp_path, "bored", start_exit=1)
    assert status == 1
    assert calls[-1] == "rm -f cid42"
