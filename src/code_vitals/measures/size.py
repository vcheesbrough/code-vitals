"""Size measures from tokei: code lines and files, by area and language."""

from collections import Counter
from collections.abc import Sequence

from code_vitals.analysis import TOKEI_VERSION
from code_vitals.measures import CommitContext, Kind, Value

TOKEI = f"tokei {TOKEI_VERSION} (--no-ignore --hidden)"


def _by_area_and_language(counts: Counter[tuple[str, str]]) -> list[Value]:
    return [
        Value(float(n), facet=language, area=area) for (area, language), n in sorted(counts.items())
    ]


class LinesOfCode:
    name = "lines_of_code"
    version = 1
    unit = "lines"
    kind = Kind.SNAPSHOT
    recomputable = True
    definition = (
        f"Code lines (not comments or blanks) per {TOKEI}, by area and language (facet), "
        "over every tracked, not excluded file tokei recognises."
    )

    def compute(self, context: CommitContext) -> Sequence[Value]:
        counts: Counter[tuple[str, str]] = Counter()
        for f in context.tokei:
            area = context.area_of(f.path)
            assert area is not None
            counts[(area, f.language)] += f.code
        return _by_area_and_language(counts)


class SourceFiles:
    name = "source_files"
    version = 1
    unit = "files"
    kind = Kind.SNAPSHOT
    recomputable = True
    definition = (
        f"Files tokei recognises ({TOKEI}), by area and language (facet), "
        "over every tracked, not excluded file."
    )

    def compute(self, context: CommitContext) -> Sequence[Value]:
        counts: Counter[tuple[str, str]] = Counter()
        for f in context.tokei:
            area = context.area_of(f.path)
            assert area is not None
            counts[(area, f.language)] += 1
        return _by_area_and_language(counts)
