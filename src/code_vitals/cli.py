"""The `code-vitals` command line. Later cards add subcommands here."""

import argparse
from collections.abc import Sequence

from code_vitals.build_info import load_build_info


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
