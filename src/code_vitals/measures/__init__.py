"""Measure plugins: each computes one measure, at one version, at one commit.

A plugin's (name, version) identifies its definition. Anything that changes
the values it records — its logic, its repo's area rules, a pinned tool such
as lizard or tokei — needs a new version (AGENTS.md review criteria). The
collector refuses a known (name, version) whose unit, kind, recomputability or
definition changed, and tool-based measures name their tool's version in their
definition, so a tool bump without a version bump is refused too.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Protocol

from code_vitals import analysis
from code_vitals.areas import AreaRuleset


class Kind(StrEnum):
    SNAPSHOT = "snapshot"  # a state at the commit: last value per period
    FLOW = "flow"  # a change made by the commit: sum per period


@dataclass(frozen=True)
class Value:
    value: float
    facet: str = ""
    area: str | None = None  # None: unsplit; otherwise an area of the repo's ruleset


@dataclass(frozen=True)
class FunctionRow:
    """One function at one commit, for complexity hotspots."""

    area: str
    file: str
    function: str
    start_line: int
    ccn: int
    nloc: int
    params: int


Output = Value | FunctionRow


@dataclass
class CommitContext:
    repo: str
    sha: str
    tree: Path  # the commit's files, extracted
    files: int  # regular files in the tree
    ruleset: AreaRuleset
    _area_cache: dict[str, str | None] = field(default_factory=dict, repr=False)

    def area_of(self, path: str) -> str | None:
        if path not in self._area_cache:
            self._area_cache[path] = self.ruleset.area_of(path)
        return self._area_cache[path]

    @cached_property
    def tokei(self) -> list[analysis.TokeiFile]:
        """tokei's per-file counts for the measured (not excluded) files."""
        return [f for f in analysis.run_tokei(self.tree) if self.area_of(f.path) is not None]

    @cached_property
    def lizard(self) -> list[analysis.LizardFile]:
        """lizard's per-file analysis of the measured files it can read."""
        paths = [
            p.relative_to(self.tree).as_posix()
            for p in self.tree.rglob("*")
            if p.is_file() and not p.is_symlink()
        ]
        return analysis.run_lizard(self.tree, [p for p in paths if self.area_of(p) is not None])


class Measure(Protocol):
    name: str
    version: int
    unit: str
    kind: Kind
    recomputable: bool
    definition: str

    def compute(self, context: CommitContext) -> Sequence[Output]: ...


def registered() -> list[Measure]:
    """Every measure the collector records, in a stable order."""
    from code_vitals.measures.complexity import (
        ComplexityTotal,
        FunctionHotspots,
        Functions,
        FunctionsOver10,
        MaxFunctionComplexity,
        Nloc,
    )
    from code_vitals.measures.files import Files
    from code_vitals.measures.size import LinesOfCode, SourceFiles

    return [
        Files(),
        LinesOfCode(),
        SourceFiles(),
        Nloc(),
        Functions(),
        ComplexityTotal(),
        FunctionsOver10(),
        MaxFunctionComplexity(),
        FunctionHotspots(),
    ]
