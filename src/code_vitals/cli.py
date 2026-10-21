"""The `code-vitals` command line. Later cards add subcommands here."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

from code_vitals.build_info import load_build_info
from code_vitals.db.migrate import migrate

DATABASE_URL_ENV = "CODE_VITALS_DATABASE_URL"
# Role password variables `migrate` reads, by role.
ROLE_PASSWORD_ENV = {
    "code_vitals_writer": "CODE_VITALS_WRITER_PASSWORD",
    "code_vitals_reader": "CODE_VITALS_READER_PASSWORD",
}


def build_parser() -> argparse.ArgumentParser:
    info = load_build_info()
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
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _migrate(environ)
    parser.print_help()
    return 0


def _migrate(environ: Mapping[str, str]) -> int:
    conninfo = environ.get(DATABASE_URL_ENV, "")
    if not conninfo:
        print(f"code-vitals migrate: {DATABASE_URL_ENV} is required", file=sys.stderr)
        return 2
    passwords = {role: environ[var] for role, var in ROLE_PASSWORD_ENV.items() if environ.get(var)}
    applied = migrate(conninfo, passwords)
    for name in applied:
        print(f"applied {name}")
    print(
        f"schema up to date ({len(applied)} applied); grants applied;"
        f" logins set for {len(passwords)} role(s)"
    )
    return 0
