"""`files`: regular files in the commit's tree. The trivial measure that proves
the collector end to end; real size measures (by area and language) arrive with
card #495."""

from collections.abc import Sequence

from code_vitals.measures import CommitContext, Kind, Value


class Files:
    name = "files"
    version = 1
    unit = "files"
    kind = Kind.SNAPSHOT
    recomputable = True
    definition = "Regular files in the commit's tree (git archive), all paths, unsplit."

    def compute(self, context: CommitContext) -> Sequence[Value]:
        return [Value(float(context.files))]
