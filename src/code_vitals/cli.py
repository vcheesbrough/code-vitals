"""The `code-vitals` command line.

stdout carries a few plain lines for whoever reads the CI step log; the
structured record of a run is its telemetry (spans and log records over
OTLP), when the OTEL_* variables configure it.
"""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from code_vitals import obs, telemetry
from code_vitals.build_info import BuildInfo, load_build_info
from code_vitals.collect import collect
from code_vitals.db.migrate import migrate
from code_vitals.repos import REPOS, RepoConfig

DATABASE_URL_ENV = "CODE_VITALS_DATABASE_URL"
# Role password variables `migrate` reads, by role.
ROLE_PASSWORD_ENV = {
    "code_vitals_writer": "CODE_VITALS_WRITER_PASSWORD",
    "code_vitals_reader": "CODE_VITALS_READER_PASSWORD",
}
# Set by Woodpecker in every step; recorded on the run's span.
PIPELINE_NUMBER_ENV = "CI_PIPELINE_NUMBER"


def build_parser(info: BuildInfo, repos: Mapping[str, RepoConfig]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-vitals",
        description="Record measures of a project's code at every trunk commit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"code-vitals {info.version} ({info.revision})",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser(
        "migrate",
        help="apply schema migrations and role grants",
        description=(
            f"Apply schema migrations and role grants to the database named by "
            f"{DATABASE_URL_ENV} (as its owner). When "
            f"{' / '.join(ROLE_PASSWORD_ENV.values())} are set, those roles get LOGIN "
            f"with that password."
        ),
    )
    collect_parser = commands.add_parser(
        "collect",
        help="record every trunk commit of a repo not yet measured",
        description=(
            f"Catch a repo up in the database named by {DATABASE_URL_ENV} (as the "
            f"writer): every trunk commit of the clone not yet measured, for every "
            f"registered measure version."
        ),
    )
    collect_parser.add_argument("--repo", required=True, choices=sorted(repos))
    collect_parser.add_argument("--clone", required=True, type=Path, help="path to a full clone")
    collect_parser.add_argument(
        "--trigger", default=str(obs.Trigger.MANUAL), choices=[str(t) for t in obs.Trigger]
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    repos: Mapping[str, RepoConfig] = REPOS,
) -> int:
    environ = os.environ if environ is None else environ
    info = load_build_info()
    parser = build_parser(info, repos)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    conninfo = environ.get(DATABASE_URL_ENV, "")
    if not conninfo:
        print(f"code-vitals {args.command}: {DATABASE_URL_ENV} is required", file=sys.stderr)
        return 2
    _log_problems_to_stderr()
    instance = args.repo if args.command == "collect" else args.command
    try:
        running = telemetry.setup(environ, info, instance)
    except telemetry.TelemetryConfigError as error:
        print(f"code-vitals: telemetry misconfigured: {error}", file=sys.stderr)
        return 2
    if not running.config.enabled:
        print(f"code-vitals: telemetry off ({running.config.reason})")
    try:
        if args.command == "migrate":
            return _migrate(conninfo, environ)
        return _collect(conninfo, repos[args.repo], args, info, environ)
    finally:
        if not running.shutdown():
            print("code-vitals: telemetry did not flush in time; some was dropped", file=sys.stderr)


def _log_problems_to_stderr() -> None:
    """Warnings and errors (with tracebacks) also reach the CI step log, in
    plain text, whether or not telemetry is on; INFO records go to OTLP only."""
    product = logging.getLogger(telemetry.PRODUCT_LOGGER)
    if any(getattr(h, "code_vitals_stderr", False) for h in product.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("code-vitals: %(levelname)s %(name)s: %(message)s"))
    handler.code_vitals_stderr = True  # type: ignore[attr-defined]
    product.addHandler(handler)


def _migrate(conninfo: str, environ: Mapping[str, str]) -> int:
    passwords = {role: environ[var] for role, var in ROLE_PASSWORD_ENV.items() if environ.get(var)}
    with obs.tracer.start_as_current_span("migrate"):
        applied = migrate(conninfo, passwords)
    for name in applied:
        print(f"applied {name}")
    print(
        f"schema up to date ({len(applied)} applied); grants applied;"
        f" logins set for {len(passwords)} role(s)"
    )
    return 0


def _collect(
    conninfo: str,
    repo: RepoConfig,
    args: argparse.Namespace,
    info: BuildInfo,
    environ: Mapping[str, str],
) -> int:
    print(f"code-vitals collect: {repo.name} from {args.clone} ({args.trigger})")
    try:
        result = collect(
            conninfo,
            repo,
            args.clone,
            obs.Trigger(args.trigger),
            info,
            pipeline_number=environ.get(PIPELINE_NUMBER_ENV) or None,
        )
    except Exception as error:
        print(f"code-vitals collect: {repo.name} failed: {error}", file=sys.stderr)
        return 1
    print(
        f"code-vitals collect: {repo.name}: {result.commits_measured} commit(s) measured"
        f" of {result.backlog} to do (run {result.run_id})"
    )
    if result.commits_failed:
        print(
            f"code-vitals collect: {repo.name}: {result.commits_failed} commit(s) failed;"
            " the next run retries them",
            file=sys.stderr,
        )
        return 1
    return 0
