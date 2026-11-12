"""The row-limit guard of superset/code_vitals_dashboard.py (card #552).

A chart with an x-axis queries a row per distinct (x-value, group), counted
over every measure's groups; Superset truncates the result past the query's
row limit without an error. `--check` (the deploy smoke) counts each such
chart's rows and fails past ROW_LIMIT_HEADROOM of its limit. These tests check
the counting SQL on its own, that it can count every chart of the bundle, and
that it counts right as the reader on a real database.
"""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from code_vitals.db.migrate import migrate

from .conftest import TEST_PASSWORDS, role_url
from .test_superset_dashboard import CHARTS, DATASETS, importer
from .test_superset_datasets import _seed


def _chart(file: str) -> dict[str, Any]:
    for data in CHARTS.values():
        if data["external_url"].endswith(f"/charts/{file}"):
            chart: dict[str, Any] = data
            return chart
    raise KeyError(file)


def _expressions(dataset: dict[str, Any]) -> dict[str, str]:
    return {c["column_name"]: c["expression"] for c in dataset["columns"] if c.get("expression")}


def _source(dataset: dict[str, Any]) -> str:
    source: str = importer.count_source(dataset["sql"], dataset["table_name"])
    return source


def test_a_count_reads_a_datasets_sql_else_its_table() -> None:
    assert importer.count_source("SELECT 1", "virtual") == "(SELECT 1) AS d"
    assert importer.count_source(None, "freshness") == "freshness"
    assert importer.count_source("", "freshness") == "freshness"


def test_a_chart_groups_by_its_x_axis_then_its_groupby() -> None:
    assert importer.grouped_by({"x_axis": "committed_at", "groupby": ["facet"]}) == [
        "committed_at",
        "facet",
    ]
    assert importer.grouped_by({"x_axis": "committed_at", "groupby": []}) == ["committed_at"]
    assert importer.grouped_by({"x_axis": "committed_at"}) == ["committed_at"]
    # A table lists rows: no x-axis, nothing grouped.
    assert importer.grouped_by({"all_columns": ["repo"], "groupby": []}) == []


def test_an_adhoc_column_is_refused() -> None:
    with pytest.raises(ValueError, match="dataset columns only"):
        importer.grouped_by({"x_axis": {"sqlExpression": "now()"}, "groupby": []})


def test_the_limit_is_the_charts_else_the_default_at_most_the_cap() -> None:
    assert importer.row_limit({"row_limit": 10000}, 50000, 100000) == 10000
    assert importer.row_limit({}, 50000, 100000) == 50000
    assert importer.row_limit({"row_limit": None}, 50000, 100000) == 50000
    assert importer.row_limit({"row_limit": 0}, 50000, 100000) == 50000
    assert importer.row_limit({"row_limit": 200000}, 50000, 100000) == 100000
    # Superset reads a limit of 0 as the cap (apply_max_row_limit).
    assert importer.row_limit({}, 0, 100000) == 100000


def test_near_the_limit_is_past_the_headroom() -> None:
    assert importer.ROW_LIMIT_HEADROOM == 0.8
    assert not importer.near_limit(40000, 50000)
    assert importer.near_limit(40001, 50000)
    assert importer.near_limit(50000, 50000)


def test_the_report_names_the_closest_chart() -> None:
    counts = [("Churn", 311, 10000), ("Operations", 7497, 50000), ("Lines", 311, 50000)]
    # scripts/test-smoke-superset.sh feeds the smoke this line.
    assert importer.row_limit_report(counts) == (
        "chart rows at most 15% of a row limit (Operations 7497 of 50000), fails past 80%"
    )
    assert importer.row_limit_report([]) == ""


def test_the_report_fails_naming_every_chart_near_its_limit() -> None:
    counts = [("Churn", 8001, 10000), ("Operations", 7497, 50000), ("Lines", 50000, 50000)]
    with pytest.raises(RuntimeError) as failure:
        importer.row_limit_report(counts)
    message = str(failure.value)
    assert message.endswith("(rows of limit): Churn 8001 of 10000; Lines 50000 of 50000")
    assert "Operations" not in message
    # At the headroom exactly, it passes.
    assert importer.row_limit_report([("Churn", 8000, 10000)]).startswith("chart rows at most 80%")


def test_the_count_groups_by_the_charts_columns_after_its_filters() -> None:
    params = {
        "x_axis": "iteration",
        "groupby": ["repo"],
        "adhoc_filters": [
            {
                "clause": "WHERE",
                "expressionType": "SIMPLE",
                "subject": "committed_at",
                "operator": "TEMPORAL_RANGE",
                "comparator": "No filter",
            },
            {
                "clause": "WHERE",
                "expressionType": "SIMPLE",
                "subject": "iteration",
                "operator": "IS NOT NULL",
                "comparator": None,
            },
        ],
    }
    assert importer.row_count_sql(params, "current_measurement", {}) == (
        'SELECT count(*) AS n FROM (SELECT 1 FROM current_measurement WHERE "iteration"'
        ' IS NOT NULL GROUP BY "iteration", "repo") AS g'
    )
    params = {"x_axis": "committed_at", "groupby": ["label"]}
    assert importer.row_count_sql(params, "t", {"label": "left(sha, 10)"}) == (
        'SELECT count(*) AS n FROM (SELECT 1 FROM t GROUP BY "committed_at", (left(sha, 10))) AS g'
    )


@pytest.mark.parametrize(
    "adhoc",
    [
        # A time range that filters, an IN list, custom SQL, a HAVING clause.
        {"clause": "WHERE", "expressionType": "SIMPLE", "subject": "committed_at"}
        | {"operator": "TEMPORAL_RANGE", "comparator": "Last week"},
        {"clause": "WHERE", "expressionType": "SIMPLE", "subject": "repo"}
        | {"operator": "IN", "comparator": ["bored"]},
        {"clause": "WHERE", "expressionType": "SQL", "sqlExpression": "true"},
        {"clause": "HAVING", "expressionType": "SIMPLE", "subject": "repo"}
        | {"operator": "IS NOT NULL"},
    ],
)
def test_a_filter_it_cannot_reproduce_is_refused(adhoc: dict[str, Any]) -> None:
    params = {"x_axis": "committed_at", "groupby": [], "adhoc_filters": [adhoc]}
    with pytest.raises(ValueError, match="cannot count"):
        importer.row_count_sql(params, "t", {})


def test_a_chart_without_an_x_axis_has_nothing_to_count() -> None:
    with pytest.raises(ValueError, match="no grouped query"):
        importer.row_count_sql({"all_columns": ["repo"]}, "t", {})


def test_every_chart_with_an_x_axis_is_guarded_and_countable() -> None:
    # Every grouped over-time chart is among them, and none uses a filter the
    # guard would refuse at deploy time.
    guarded = []
    for data in CHARTS.values():
        params = data["params"]
        if not importer.grouped_by(params):
            assert params["viz_type"] == "table", data["slice_name"]
            continue
        dataset = DATASETS[data["dataset_uuid"]]
        importer.row_count_sql(params, _source(dataset), _expressions(dataset))
        guarded.append(data["external_url"].rsplit("/", 1)[1])
    assert {
        "operations_by_kind.yaml",
        "e2e_scenarios_by_suite.yaml",
        "size_by_area.yaml",
        "complexity_by_area.yaml",
        "churn_by_area.yaml",
        "average_complexity_by_repo.yaml",
        "lines_by_iteration.yaml",
        "churn_by_iteration.yaml",
    } <= set(guarded)
    assert len(guarded) == len(CHARTS) - 2  # all but the Freshness and Hotspots tables


@pytest.fixture
def db(fresh_db: str) -> Iterator[str]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db


@pytest.mark.parametrize(
    ("file", "rows"),
    [
        # tests/test_superset_datasets.py's seed: moments 01:00-05:00, with
        # bored's areas server and tests (tests gone at 05:00), v-note's web
        # from 02:00, carried forward.
        ("size_by_area.yaml", 2 + 3 + 3 + 3 + 2),
        # Per repo: bored at all five moments, v-note from 02:00.
        ("average_complexity_by_repo.yaml", 5 + 4),
        # Flows at each commit time only, by area: two bored commits at 03:00.
        ("churn_by_area.yaml", 2 + 1 + 2 + 1 + 1),
        # (iteration, repo): bored 1, 2, 3 and v-note 1, 2.
        ("lines_by_iteration.yaml", 5),
        # A tile's trendline: one point per moment.
        ("tile_lines.yaml", 5),
    ],
)
def test_the_count_is_the_charts_rows_as_the_reader(db: str, file: str, rows: int) -> None:
    _seed(db)
    chart = _chart(file)
    dataset = DATASETS[chart["dataset_uuid"]]
    sql = importer.row_count_sql(chart["params"], _source(dataset), _expressions(dataset))
    with psycopg.connect(role_url(db, "code_vitals_reader")) as conn:
        row = conn.execute(sql).fetchone()
    assert row == (rows,)
