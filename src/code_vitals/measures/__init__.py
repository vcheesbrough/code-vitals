"""Measure plugins: each computes one measure, at one version, at one commit.

A plugin's (name, version) identifies its definition. Anything that changes
the values it records — its logic, its area rules, a pinned tool such as lizard
or tokei — needs a new version (AGENTS.md review criteria); the collector
refuses a known (name, version) whose unit, kind or recomputability changed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Kind(StrEnum):
    SNAPSHOT = "snapshot"  # a state at the commit: last value per period
    FLOW = "flow"  # a change made by the commit: sum per period


@dataclass(frozen=True)
class Value:
    value: float
    facet: str = ""
    # Area-split values arrive with area rulesets (card #495).
    area: str | None = None


@dataclass(frozen=True)
class CommitContext:
    repo: str
    sha: str
    tree: Path  # the commit's files, extracted
    files: int  # regular files in the tree


class Measure(Protocol):
    name: str
    version: int
    unit: str
    kind: Kind
    recomputable: bool
    definition: str

    def compute(self, context: CommitContext) -> Sequence[Value]: ...


def registered() -> list[Measure]:
    """Every measure the collector records, in a stable order."""
    from code_vitals.measures.files import Files

    return [Files()]
