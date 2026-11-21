"""The `code-vitals` command line.

stdout carries a few plain lines for whoever reads the CI step log; the
structured record of a run is its telemetry (spans and log records over
OTLP), when the OTEL_* variables configure it.
"""

import argparse
import dataclasses
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

import psycopg

from code_vitals import obs, plugin, settings, telemetry
from code_vitals.backfill import Outcome, RegisteredRepo, backfill_repo, registered_repos
from code_vitals.build_info import BuildInfo, load_build_info
from code_vitals.checks import check_status
from code_vitals.collect import collect, skip_repo
from code_vitals.db.migrate import migrate
from code_vitals.repos import RepoConfig
from code_vitals.retire import RetireRefused, retire

DATABASE_URL_ENV = "CODE_VITALS_DATABASE_URL"
# Role password variables `migrate` reads, by role.
ROLE_PASSWORD_ENV = {
    "code_vitals_writer": "CODE_VITALS_WRITER_PASSWORD",
    "code_vitals_reader": "CODE_VITALS_READER_PASSWORD",
}
# Set by Woodpecker in every step; recorded on the run's span.
PIPELINE_NUMBER_ENV = "CI_PIPELINE_NUMBER"
# A repo's name: also its telemetry instance, and a word in shell scripts.
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


TOKEN_STDIN = "--clone-token-stdin"
TOKEN_STDIN_HELP = (
    "read a private repo's clone token from the first line of stdin (never an"
    " argument or a variable, which `docker inspect` would show)"
)

# `backfill-repo`'s exit status for a repo without a plugin step: skipped,
# which scripts/run-collector.sh tells apart from a failure.
SKIPPED_EXIT = 3


def build_parser(info: BuildInfo) -> argparse.ArgumentParser:
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
            f"registered measure version. The repo's plugin settings come from "
            f"{settings.SETTINGS_ENV} (JSON, as the plugin passes them)."
        ),
    )
    collect_parser.add_argument("--repo", required=True, type=_repo_name)
    collect_parser.add_argument("--url", help="where the repo is cloned from (recorded)")
    source = collect_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--clone", type=Path, help="path to a full clone")
    source.add_argument(
        "--fetch",
        action="store_true",
        help="clone the repo from --url first (a temporary full clone)",
    )
    collect_parser.add_argument(
        TOKEN_STDIN,
        action="store_true",
        help=TOKEN_STDIN_HELP + "; with --fetch, the repo is recorded private, without it public",
    )
    collect_parser.add_argument(
        "--trigger", default=str(obs.Trigger.MANUAL), choices=[str(t) for t in obs.Trigger]
    )
    commands.add_parser(
        "list-repos",
        help="list the registered repos a deployment re-measures",
        description=(
            "One `repo <name> <url> <trunk> <private|public>` line per registered repo with"
            " a URL. A repo without one is marked skipped."
        ),
    )
    backfill_parser = commands.add_parser(
        "backfill-repo",
        help="re-measure and check one registered repo after a deployment",
        description=(
            "Clone the repo, read its code-vitals plugin settings from"
            f" {settings.WORKFLOW_PATH} at the trunk head, catch it up with"
            " trigger=backfill, then check it: a second catch-up must find nothing to do,"
            " and the values at its fixed commits of expected_values.toml (if any) must"
            f" match. Exits {SKIPPED_EXIT} when the repo has no plugin step: it is marked"
            " skipped, so measure versions complete without it until it is caught up again."
        ),
    )
    backfill_parser.add_argument("--repo", required=True, type=_repo_name)
    backfill_parser.add_argument("--url", required=True)
    backfill_parser.add_argument("--trunk", required=True)
    backfill_parser.add_argument(TOKEN_STDIN, action="store_true", help=TOKEN_STDIN_HELP)
    status_parser = commands.add_parser(
        "check-status",
        help="check the repos a deployment collected",
        description=(
            "Every measure version this build registers must be complete (unless every"
            " registered repo is skipped), and the last run of every repo named must have"
            " succeeded."
        ),
    )
    status_parser.add_argument("--repo", action="append", default=[], type=_repo_name)
    retire_parser = commands.add_parser(
        "retire",
        help="retire a measure version this build no longer records",
        description=(
            "Mark a measure version retired, so the views stop reading it. --delete-rows also"
            f" deletes its values, which needs the owner's {DATABASE_URL_ENV}."
        ),
    )
    retire_parser.add_argument("measure")
    retire_parser.add_argument("version", type=int)
    retire_parser.add_argument("--delete-rows", action="store_true")
    commands.add_parser(
        "plugin",
        help="run as the Woodpecker plugin (the code-vitals-plugin image's entrypoint)",
        description=(
            "Validate the step's settings (PLUGIN_* variables) and run this build's collector"
            " for the repo on code-vitals' prod network through the docker socket."
        ),
    )
    return parser


def _repo_name(value: str) -> str:
    if not REPO_NAME.match(value):
        raise argparse.ArgumentTypeError(f"not a repo name: {value!r}")
    return value


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    info = load_build_info()
    parser = build_parser(info)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "plugin":
        # Needs no database of its own: the collector it starts has one.
        return plugin.run(environ, info)

    conninfo = environ.get(DATABASE_URL_ENV, "")
    if not conninfo:
        print(f"code-vitals {args.command}: {DATABASE_URL_ENV} is required", file=sys.stderr)
        return 2
    repo_settings = None
    if args.command == "collect":
        try:
            repo_settings = settings.from_json(environ.get(settings.SETTINGS_ENV, ""))
        except settings.SettingsError as error:
            print(f"code-vitals collect: invalid settings: {error}", file=sys.stderr)
            return 2
    token = None
    if getattr(args, "clone_token_stdin", False):
        token = (stdin or sys.stdin).readline().strip()
        if not token:
            print(
                f"code-vitals {args.command}: {TOKEN_STDIN} but no token on stdin", file=sys.stderr
            )
            return 2
    _log_problems_to_stderr()
    instance = args.repo if args.command in ("collect", "backfill-repo") else args.command
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
        if args.command == "list-repos":
            return _list_repos(conninfo)
        if args.command == "backfill-repo":
            return _backfill_repo(conninfo, args, info, environ, token)
        if args.command == "check-status":
            return _check_status(conninfo, args.repo)
        if args.command == "retire":
            return _retire(conninfo, args)
        assert repo_settings is not None
        repo = repo_settings.repo(args.repo, args.url)
        if args.fetch:
            # A plugin run: it cloned the repo with a token exactly when it is private.
            repo = dataclasses.replace(repo, private=token is not None)
        return _collect(conninfo, repo, args, info, environ, token)
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
    token: str | None,
) -> int:
    source = repo.url if args.fetch else args.clone
    if args.fetch and repo.url is None:
        print("code-vitals collect: --fetch needs --url", file=sys.stderr)
        return 2
    print(f"code-vitals collect: {repo.name} from {source} ({args.trigger})")
    try:
        result = collect(
            conninfo,
            repo,
            args.clone,  # None with --fetch: collect clones the repo's URL
            obs.Trigger(args.trigger),
            info,
            pipeline_number=environ.get(PIPELINE_NUMBER_ENV) or None,
            clone_token=token,
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


def _report(command: str, problems: Sequence[str]) -> int:
    for problem in problems:
        print(f"code-vitals {command}: {problem}", file=sys.stderr)
    if problems:
        print(f"code-vitals {command}: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"code-vitals {command}: ok")
    return 0


def _list_repos(conninfo: str) -> int:
    with obs.tracer.start_as_current_span("list-repos"):
        repos = registered_repos(conninfo)
        for repo in repos:
            if repo.url is None:
                # Never cloned by a deployment, so never backfilled by one.
                skip_repo(conninfo, repo.name)
    for repo in repos:
        if repo.url is None:
            print(f"code-vitals list-repos: {repo.name} has no URL; deployments skip it")
        else:
            access = "private" if repo.private else "public"
            print(f"repo {repo.name} {repo.url} {repo.trunk} {access}")
    return 0


def _backfill_repo(
    conninfo: str,
    args: argparse.Namespace,
    info: BuildInfo,
    environ: Mapping[str, str],
    token: str | None,
) -> int:
    name = args.repo
    print(f"code-vitals backfill-repo: {name} from {args.url} ({args.trunk})")
    with obs.tracer.start_as_current_span("backfill-repo", attributes={obs.REPO: name}):
        try:
            result = backfill_repo(
                conninfo,
                RegisteredRepo(name, args.url, args.trunk, private=token is not None),
                info,
                pipeline_number=environ.get(PIPELINE_NUMBER_ENV) or None,
                clone_token=token,
            )
        except Exception as error:
            print(f"code-vitals backfill-repo: {name} failed: {error}", file=sys.stderr)
            return 1
    if result.outcome is Outcome.SKIPPED:
        print(
            f"code-vitals backfill-repo: {name} has no code-vitals plugin step in"
            f" {settings.WORKFLOW_PATH} at {args.trunk}; skipped"
        )
        return SKIPPED_EXIT
    assert result.collected is not None
    collected = result.collected
    print(
        f"code-vitals backfill-repo: {name}: {collected.commits_measured} commit(s) measured"
        f" of {collected.backlog} to do (run {collected.run_id})"
    )
    if collected.commits_failed:
        print(
            f"code-vitals backfill-repo: {name}: {collected.commits_failed} commit(s) failed",
            file=sys.stderr,
        )
    status = _report("backfill-repo", result.problems)
    return 1 if collected.commits_failed else status


def _check_status(conninfo: str, repos: Sequence[str]) -> int:
    with obs.tracer.start_as_current_span("check-status"):
        try:
            problems = check_status(conninfo, repos)
        except Exception as error:
            problems = [f"could not be checked: {error}"]
    return _report("check-status", problems)


def _retire(conninfo: str, args: argparse.Namespace) -> int:
    with obs.tracer.start_as_current_span("retire", attributes={obs.MEASURE: args.measure}):
        try:
            done = retire(conninfo, args.measure, args.version, delete_rows=args.delete_rows)
        except (RetireRefused, psycopg.Error) as error:
            print(f"code-vitals retire: {error}", file=sys.stderr)
            return 1
    print(
        f"code-vitals retire: {args.measure} v{args.version} retired (was {done.previous_status});"
        f" {done.rows_deleted} row(s) deleted"
    )
    return 0
