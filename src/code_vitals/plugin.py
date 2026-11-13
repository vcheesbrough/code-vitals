"""The code-vitals Woodpecker plugin: how a repo measures itself.

A repo adds one plugin step (README § Measuring a repo). Woodpecker runs this
image with the step's `settings:` as PLUGIN_* variables and the docker socket
mounted. It validates the settings (code_vitals.settings), then starts the
collector of the same build on code-vitals' prod network through the socket —
a plugin step's own network reaches neither Postgres nor the OTLP collector —
and passes the collector's exit status on. The collector clones the public
repo itself (`collect --fetch`, full history and tags) and records the repo,
its URL and every trunk commit not yet measured.

Secrets reach the collector by name (`-e NAME`), never on a command line.
Nothing here reads stdin: Woodpecker feeds a step's commands to its shell
that way, and a plugin step must not depend on it either.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from urllib.parse import quote

from code_vitals import settings
from code_vitals.build_info import BuildInfo

COLLECTOR_IMAGE = "registry.desync.link/code-vitals-collector"
DB_NETWORK = "code-vitals_default"
DB_HOST = "code-vitals-db"
TELEMETRY_NETWORK = "proxy-backend"
TELEMETRY = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://monitor-alloy:4318",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=prod",
}
# Set by Woodpecker in every step.
REPO_NAME_ENV = "CI_REPO_NAME"
REPO_URL_ENV = "CI_REPO_CLONE_URL"
PIPELINE_NUMBER_ENV = "CI_PIPELINE_NUMBER"
PASSWORD_ENV = settings.PLUGIN_ENV_PREFIX + settings.WRITER_PASSWORD.upper()

_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_URL = re.compile(r"^https?://[^@\s]+$")


class PluginError(ValueError):
    """The step is misconfigured; the message says how."""


def collector_image(build: BuildInfo) -> str:
    """The collector this plugin build starts: the one published with it,
    under the same version. Versions are never republished, so the tag is as
    fixed as a digest and is pulled only when missing."""
    return f"{COLLECTOR_IMAGE}:{build.version}"


def collector_env(environ: Mapping[str, str]) -> tuple[str, str, dict[str, str]]:
    """The repo's name and URL, and the variables the collector container gets
    by name. Raises PluginError or SettingsError on a misconfigured step."""
    found = settings.from_plugin_env(environ)
    name = environ.get(REPO_NAME_ENV, "")
    if not _NAME.match(name):
        raise PluginError(f"{REPO_NAME_ENV} must be the repo's name, not {name!r}")
    url = environ.get(REPO_URL_ENV, "")
    if not _URL.match(url):
        raise PluginError(
            f"{REPO_URL_ENV} must be the repo's public http(s) clone URL, not {url!r}"
        )
    password = environ.get(PASSWORD_ENV, "")
    if not password:
        raise PluginError(
            "the writer_password setting is required"
            " (writer_password: {from_secret: code_vitals_prod_writer_password})"
        )
    env = {
        "CODE_VITALS_DATABASE_URL": (
            f"postgresql://code_vitals_writer:{quote(password, safe='')}@{DB_HOST}:5432/code_vitals"
        ),
        settings.SETTINGS_ENV: found.as_json(),
        **TELEMETRY,
        PIPELINE_NUMBER_ENV: environ.get(PIPELINE_NUMBER_ENV, ""),
    }
    return name, url, env


def run(
    environ: Mapping[str, str],
    build: BuildInfo,
    *,
    docker: str = "docker",
) -> int:
    """Validate the step, run the collector, and return its exit status (2
    for a misconfigured step)."""
    try:
        name, url, env = collector_env(environ)
    except (PluginError, settings.SettingsError) as error:
        print(f"code-vitals plugin: invalid step: {error}", file=sys.stderr)
        return 2
    image = collector_image(build)
    found = json.loads(env[settings.SETTINGS_ENV])
    print(
        f"code-vitals plugin {build.version}: {name} from {url}, trunk {found['trunk']},"
        f" area ruleset v{found['areas']['version']}; starting {image}",
        flush=True,
    )
    # Only what the collector needs, plus what docker itself reads; the plugin
    # settings (the password among them) stay out of the docker CLI's env.
    docker_env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    docker_env.update(env)
    names = [flag for var in env for flag in ("-e", var)]

    def call(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [docker, *args],
            env=docker_env,
            stdin=subprocess.DEVNULL,
            # Otherwise inherited: the collector's output is the step log.
            stdout=subprocess.PIPE if capture else None,
            text=True,
            check=False,
        )

    # --rm: the daemon removes the container whenever it exits, even if this
    # step is cancelled first; the finally covers a failure before it starts.
    created = call(
        "create",
        "--rm",
        "--pull",
        "missing",
        "--network",
        DB_NETWORK,
        *names,
        image,
        "collect",
        "--repo",
        name,
        "--url",
        url,
        "--fetch",
        "--trigger",
        "push",
        capture=True,
    )
    if created.returncode != 0:
        print(f"code-vitals plugin: could not create the collector ({image})", file=sys.stderr)
        return created.returncode
    cid = created.stdout.strip()
    try:
        # `docker run` joins one network; the OTLP collector is on another.
        connected = call("network", "connect", TELEMETRY_NETWORK, cid)
        if connected.returncode != 0:
            return connected.returncode
        return call("start", "--attach", cid).returncode
    finally:
        subprocess.run(
            [docker, "rm", "-f", cid],
            env=docker_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
