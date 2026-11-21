"""The dashboard charts' row limits, checked where the data arrives (card #553).

A dashboard chart with an x-axis queries a row per x-value and group, and
Superset cuts the result off past its row limit without an error. A deploy's
smoke counts those rows (`code_vitals_dashboard --check`, card #552), but the
rows grow with every measured repo's trunk pipeline, which never deploys
code-vitals. So `collect` counts them as well, once it has recorded its run,
and fails past the same headroom: the code-vitals step of the push whose data
takes a chart there fails (never blocking that repo's pipeline, where the step
is `failure: ignore`), and says which chart.

The collector does not read Superset's bundle: chart_rows.toml is its copy of
each such chart's count and limit, rendered from superset/dashboard/ by
`code_vitals_dashboard --row-limits` and held equal to it by
tests/test_row_limits.py. It counts what Superset would read, as the collector's
role (roles.sql lets the writer read the reader's views), each dataset once.
"""

import logging
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources

import psycopg

from code_vitals import obs

log = logging.getLogger("code_vitals.row_limits")

MANIFEST = "chart_rows.toml"


@dataclass(frozen=True)
class Chart:
    file: str
    name: str
    dataset: str
    limit: int
    # Counts the chart's rows from its dataset, read as Manifest.source.
    count: str


@dataclass(frozen=True)
class Manifest:
    headroom: float
    source: str
    datasets: dict[str, str]
    charts: tuple[Chart, ...]


@dataclass(frozen=True)
class ChartRows:
    chart: Chart
    rows: int

    @property
    def share(self) -> float:
        return self.rows / self.chart.limit


@dataclass(frozen=True)
class RowLimitCheck:
    counts: tuple[ChartRows, ...]
    # The charts past the headroom, each one problem.
    problems: tuple[str, ...]
    summary: str


def load_manifest(text: str | None = None) -> Manifest:
    if text is None:
        text = (resources.files("code_vitals") / MANIFEST).read_text("utf-8")
    data = tomllib.loads(text)
    return Manifest(
        headroom=float(data["headroom"]),
        source=str(data["source"]),
        datasets={str(name): str(entry["sql"]) for name, entry in data["datasets"].items()},
        charts=tuple(
            Chart(
                file=str(c["file"]),
                name=str(c["name"]),
                dataset=str(c["dataset"]),
                limit=int(c["limit"]),
                count=str(c["count"]),
            )
            for c in data["charts"]
        ),
    )


def dataset_sql(manifest: Manifest, dataset: str, charts: Sequence[Chart]) -> str:
    """One query counting every chart on `dataset`: the dataset's SQL is
    evaluated once, as the CTE the charts' counts read."""
    counts = ",\n       ".join(f"({chart.count})" for chart in charts)
    return (
        f"WITH {manifest.source} AS MATERIALIZED (\n{manifest.datasets[dataset]}\n)\n"
        f"SELECT {counts}"
    )


def count_rows(conninfo: str, manifest: Manifest) -> list[ChartRows]:
    """The rows each chart of the manifest queries now, in its order."""
    rows: dict[str, int] = {}
    with psycopg.connect(conninfo) as conn:
        for dataset in sorted({c.dataset for c in manifest.charts}):
            charts = [c for c in manifest.charts if c.dataset == dataset]
            with obs.db_span("SELECT", "count chart rows") as span:
                span.set_attribute(obs.DATASET, dataset)
                found = conn.execute(dataset_sql(manifest, dataset, charts)).fetchone()
            assert found is not None
            rows.update((c.file, int(str(n))) for c, n in zip(charts, found, strict=True))
    return [ChartRows(c, rows[c.file]) for c in manifest.charts]


def near_limit(rows: int, limit: int, headroom: float) -> bool:
    """Whether a query of `rows` rows is past `headroom` of `limit`."""
    return rows > limit * headroom


def _describe(count: ChartRows) -> str:
    return f"{count.chart.name} ({count.chart.file}) {count.rows} of {count.chart.limit}"


def report(counts: Sequence[ChartRows], headroom: float) -> RowLimitCheck:
    """A problem per chart near its limit, and a line naming the chart closest
    to its limit ("" for no charts)."""
    problems = tuple(
        f"chart {_describe(c)} rows: past {headroom:.0%} of its row limit, past which Superset"
        " truncates it without an error; raise its row_limit or coarsen its x-axis in"
        " superset/dashboard/"
        for c in counts
        if near_limit(c.rows, c.chart.limit, headroom)
    )
    summary = ""
    if counts:
        closest = max(counts, key=lambda c: c.share)
        summary = (
            f"chart rows at most {closest.share:.0%} of a row limit ({_describe(closest)}),"
            f" fails past {headroom:.0%}"
        )
    return RowLimitCheck(tuple(counts), problems, summary)


def check_row_limits(
    conninfo: str, repo: str, *, manifest: Manifest | None = None
) -> RowLimitCheck:
    """Count every chart's rows and report those near their limit (a warning
    each, which also reaches the step log); raises if they cannot be counted."""
    manifest = load_manifest() if manifest is None else manifest
    with obs.tracer.start_as_current_span("check-row-limits", attributes={obs.REPO: repo}) as span:
        checked = report(count_rows(conninfo, manifest), manifest.headroom)
        span.set_attribute(obs.CHARTS_NEAR_LIMIT, len(checked.problems))
        for problem in checked.problems:
            log.warning("%s", problem, extra={obs.REPO: repo})
        if checked.summary:
            log.info("%s", checked.summary, extra={obs.REPO: repo})
    return checked
