"""Complexity measures from lizard, by area.

`nloc` is lizard's non-comment lines of code over the files it analyses — the
denominator the prototype's "complexity per 1,000 lines" used, so Superset
divides `complexity_total` by `nloc`, not by tokei's `lines_of_code`.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence

from code_vitals.analysis import LIZARD_VERSION, LizardFunction
from code_vitals.measures import CommitContext, FunctionRow, Kind, Value

LIZARD = f"lizard {LIZARD_VERSION}"
THRESHOLD = 10


def _per_area(
    context: CommitContext, reduce: Callable[[list[LizardFunction], int], float]
) -> list[Value]:
    """One value per area that has lizard-readable files: reduce(functions, nloc)."""
    functions: dict[str, list[LizardFunction]] = defaultdict(list)
    nloc: dict[str, int] = defaultdict(int)
    for f in context.lizard:
        area = context.area_of(f.path)
        assert area is not None
        functions[area].extend(f.functions)
        nloc[area] += f.nloc
    return [Value(float(reduce(functions[a], nloc[a])), area=a) for a in sorted(nloc)]


class _LizardMeasure:
    version = 1
    kind = Kind.SNAPSHOT
    recomputable = True


class Nloc(_LizardMeasure):
    name = "nloc"
    unit = "lines"
    definition = f"Non-comment lines of code of the files {LIZARD} analyses, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda _fns, nloc: nloc)


class Functions(_LizardMeasure):
    name = "functions"
    unit = "functions"
    definition = f"Functions and methods {LIZARD} finds, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda fns, _nloc: len(fns))


class ComplexityTotal(_LizardMeasure):
    name = "complexity_total"
    unit = "ccn"
    definition = f"Sum of cyclomatic complexity over every function {LIZARD} finds, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda fns, _nloc: sum(f.ccn for f in fns))


class FunctionsOver10(_LizardMeasure):
    name = "functions_over_10"
    unit = "functions"
    definition = f"Functions with cyclomatic complexity over {THRESHOLD} per {LIZARD}, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda fns, _nloc: sum(1 for f in fns if f.ccn > THRESHOLD))


class MaxFunctionComplexity(_LizardMeasure):
    name = "max_function_complexity"
    unit = "ccn"
    definition = f"Highest cyclomatic complexity of any one function per {LIZARD}, by area."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return _per_area(context, lambda fns, _nloc: max((f.ccn for f in fns), default=0))


class FunctionHotspots(_LizardMeasure):
    name = "function_complexity"
    unit = "functions"
    definition = (
        f"One function_complexity row per function {LIZARD} finds (file, name, start line, "
        "ccn, nloc, params, area); no measurement values."
    )

    def compute(self, context: CommitContext) -> Sequence[FunctionRow]:
        rows: list[FunctionRow] = []
        for f in context.lizard:
            area = context.area_of(f.path)
            assert area is not None
            rows.extend(
                FunctionRow(area, f.path, fn.name, fn.start_line, fn.ccn, fn.nloc, fn.params)
                for fn in f.functions
            )
        return rows
