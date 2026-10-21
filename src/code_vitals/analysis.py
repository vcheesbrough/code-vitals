"""Per-commit analyses shared by several measures, each run at most once per
commit: tokei (lines by language) and lizard (functions and complexity).

The tools are pinned (tokei in tool-versions.env, lizard in pyproject.toml);
a measure built on one names that version in its definition, so bumping a
tool without a measure version bump is refused by the collector.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import lizard

from code_vitals import obs

LIZARD_VERSION = lizard.version
# The tokei the measures are defined against (tool-versions.env pins the
# binary); run_tokei refuses any other, so a tokei bump cannot silently change
# values without a measure version bump.
TOKEI_VERSION = "15.0.0"
TOKEI_FLAGS = ("--output", "json", "--files", "--no-ignore", "--hidden")


class AnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokeiFile:
    path: str  # relative to the tree root, `/`-separated
    language: str
    code: int
    comments: int
    blanks: int


@dataclass(frozen=True)
class LizardFunction:
    name: str
    start_line: int
    ccn: int
    nloc: int
    params: int


@dataclass(frozen=True)
class LizardFile:
    path: str
    language: str
    nloc: int
    functions: tuple[LizardFunction, ...]


def tokei_version() -> str:
    out = subprocess.run(["tokei", "--version"], capture_output=True, text=True, check=True).stdout
    return out.split()[1]


_checked_tokei: list[str] = []


def run_tokei(tree: Path) -> list[TokeiFile]:
    """Every file tokei recognises, per language; embedded languages (code
    blocks in Markdown and the like) are counted in their host file's language."""
    if not _checked_tokei:
        found = tokei_version()
        if found != TOKEI_VERSION:
            raise AnalysisError(
                f"tokei {found} installed; the measures are defined for {TOKEI_VERSION}"
            )
        _checked_tokei.append(found)
    with obs.tracer.start_as_current_span("tokei"):
        result = subprocess.run(
            ["tokei", *TOKEI_FLAGS, str(tree)], capture_output=True, text=True, check=False
        )
    if result.returncode != 0:
        raise AnalysisError(f"tokei failed: {result.stderr.strip()}")
    files = []
    for language, stats in json.loads(result.stdout).items():
        if language == "Total":
            continue
        # Code embedded in a file of this language (a ```rust block in
        # Markdown, <script> in HTML) is reported under `children`, by the
        # host file's name; add it back to that file, in the host's language.
        embedded: dict[str, list[int]] = {}
        for child_reports in stats.get("children", {}).values():
            for report in child_reports:
                s = report["stats"]
                totals = embedded.setdefault(report["name"], [0, 0, 0])
                totals[0] += int(s["code"])
                totals[1] += int(s["comments"])
                totals[2] += int(s["blanks"])
        for report in stats.get("reports", []):
            s = report["stats"]
            extra = embedded.get(report["name"], [0, 0, 0])
            files.append(
                TokeiFile(
                    path=Path(report["name"]).relative_to(tree).as_posix(),
                    language=language,
                    code=int(s["code"]) + extra[0],
                    comments=int(s["comments"]) + extra[1],
                    blanks=int(s["blanks"]) + extra[2],
                )
            )
    return sorted(files, key=lambda f: f.path)


def run_lizard(tree: Path, paths: list[str]) -> list[LizardFile]:
    """lizard on every given file it has a reader for."""
    files = []
    with obs.tracer.start_as_current_span("lizard"):
        for path in sorted(paths):
            full = tree / path
            reader = lizard.get_reader_for(str(full))
            if reader is None or full.is_symlink() or not full.is_file():
                continue
            info = lizard.analyze_file(str(full))
            files.append(
                LizardFile(
                    path=path,
                    language=reader.language_names[0],
                    nloc=int(info.nloc),
                    functions=tuple(
                        LizardFunction(
                            name=f.name,
                            start_line=int(f.start_line),
                            ccn=int(f.cyclomatic_complexity),
                            nloc=int(f.nloc),
                            params=int(f.parameter_count),
                        )
                        for f in info.function_list
                    ),
                )
            )
    return files
