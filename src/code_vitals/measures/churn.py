"""Churn: what each trunk commit changed against its first parent, by area.

Flow measures (sum per period). Lines come from `git diff --numstat
--no-renames`, so a rename is a delete plus an add; a binary file counts as a
changed file with no lines; excluded paths (lock files, generated output) do
not count.
"""

from collections import Counter
from collections.abc import Callable, Sequence

from code_vitals.gitrepo import FileChange
from code_vitals.measures import CommitContext, Kind, Value

_SOURCE = "git diff --numstat --no-renames against the first parent (empty tree for a root commit)"


def _per_area(context: CommitContext, count: Callable[[FileChange], int]) -> list[Value]:
    totals: Counter[str] = Counter()
    for change in context.changes:
        area = context.area_of(change.path)
        assert area is not None
        totals[area] += count(change)
    return [Value(float(n), area=area) for area, n in sorted(totals.items())]


class _Churn:
    version = 1
    kind = Kind.FLOW
    recomputable = True


class LinesAdded(_Churn):
    name = "lines_added"
    unit = "lines"
    definition = f"Lines added by the commit per {_SOURCE}, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda c: c.added)


class LinesDeleted(_Churn):
    name = "lines_deleted"
    unit = "lines"
    definition = f"Lines deleted by the commit per {_SOURCE}, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda c: c.deleted)


class FilesChanged(_Churn):
    name = "files_changed"
    unit = "files"
    definition = f"Files added, modified or deleted by the commit per {_SOURCE}, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda c: 1)
