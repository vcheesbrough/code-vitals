"""superset/dashboard/: the dashboard as code (card #500).

The bundle is Superset's import/export format, imported on every deploy by
superset/code_vitals_dashboard.py (loaded here by path, like
code_vitals_auth.py; it imports Superset only inside import_bundle). These
tests check the bundle on its own: it reads only the views the reader role may
read, every reference resolves, every chart is on the dashboard, the filters
cover repo, area, commit time and iteration on every chart they can apply to,
the over-time charts have a point per commit (card #549), and the database
file carries no secret. tests/test_superset_datasets.py runs the datasets' SQL.
"""

import ast
import importlib.util
import json
import re
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from code_vitals.measures import Kind, registered

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "superset" / "dashboard"


def _load() -> ModuleType:
    path = ROOT / "superset" / "code_vitals_dashboard.py"
    spec = importlib.util.spec_from_file_location("code_vitals_dashboard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


importer = _load()
CONTENTS: dict[str, str] = importer.read_bundle(BUNDLE)
FILES: dict[str, Any] = {name: yaml.safe_load(text) for name, text in CONTENTS.items()}


def _of(prefix: str) -> dict[str, Any]:
    return {name: data for name, data in FILES.items() if name.startswith(prefix)}


DATABASE = FILES["databases/code-vitals.yaml"]
DATASETS = {data["uuid"]: data for data in _of("datasets/").values()}
CHARTS = {data["uuid"]: data for data in _of("charts/").values()}
DASHBOARDS = list(_of("dashboards/").values())
DASHBOARD = DASHBOARDS[0]
POSITION: dict[str, Any] = DASHBOARD["position"]
PLACED: dict[str, int] = {
    item["meta"]["uuid"]: item["meta"]["chartId"]
    for item in POSITION.values()
    if isinstance(item, dict) and item.get("type") == "CHART"
}
FILTERS = {f["name"]: f for f in DASHBOARD["metadata"]["native_filter_configuration"]}
KINDS = {m.name: m.kind for m in registered()}


def _reader_views() -> set[str]:
    """What roles.sql lets the reader role SELECT."""
    sql = (ROOT / "src" / "code_vitals" / "db" / "roles.sql").read_text()
    match = re.search(r"GRANT SELECT\s+ON ([\w\s,]+?)\s+TO code_vitals_reader", sql)
    assert match, "roles.sql grants the reader nothing"
    return {name.strip() for name in match[1].split(",")}


def _chart_id(key: str) -> int:
    """The bundle's chartId of the chart in charts/<key>.yaml."""
    return PLACED[FILES[f"charts/{key}.yaml"]["uuid"]]


def test_bundle_is_a_dashboard_export() -> None:
    assert FILES["metadata.yaml"]["type"] == "Dashboard"
    assert len(DASHBOARDS) == 1
    assert DASHBOARD["slug"] == "code-vitals"
    assert DASHBOARD["published"] is True
    for name, data in FILES.items():
        if name != "metadata.yaml":
            assert data["version"] == "1.0.0", name


def test_every_uuid_is_unique_and_valid() -> None:
    uuids = [data["uuid"] for name, data in FILES.items() if name != "metadata.yaml"]
    assert len(uuids) == len(set(uuids))
    for value in uuids:
        uuid.UUID(value)


def test_everything_but_the_database_is_managed_externally() -> None:
    # Superset shows these as managed in git and points at the source file.
    for name, data in FILES.items():
        if name.split("/")[0] in {"datasets", "charts", "dashboards"}:
            assert data["is_managed_externally"] is True, name
            assert data["external_url"].endswith(f"/superset/dashboard/{name}"), name


def test_the_database_file_carries_no_secret() -> None:
    assert DATABASE["database_name"] == importer.DATABASE_NAME
    # Superset's password mask: the deploy never imports this file, and the
    # real URI comes from set-database-uri.
    match = re.fullmatch(
        r"postgresql\+psycopg2://code_vitals_reader:([^@]*)@[^/]+/code_vitals",
        DATABASE["sqlalchemy_uri"],
    )
    assert match and match[1] == "X" * 10
    assert not {"password", "encrypted_extra", "masked_encrypted_extra", "ssh_tunnel"} & set(
        DATABASE
    )
    assert DATABASE["allow_dml"] is False


def test_no_file_carries_a_password() -> None:
    for name, text in CONTENTS.items():
        if name != "databases/code-vitals.yaml":
            assert "password" not in text.lower(), name
            assert "postgresql" not in text, name


def test_datasets_use_the_code_vitals_connection() -> None:
    for data in DATASETS.values():
        assert data["database_uuid"] == DATABASE["uuid"], data["table_name"]


def _tables_read(sql: str) -> set[str]:
    sql = re.sub(r"--[^\n]*", "", sql)
    ctes = set(
        re.findall(
            r"(?:WITH|,)\s*(\w+)\s+AS\s+(?:(?:NOT\s+)?MATERIALIZED\s*)?\(", sql, flags=re.IGNORECASE
        )
    )
    tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", sql, flags=re.IGNORECASE))
    return tables - ctes


def test_datasets_read_only_the_reader_views() -> None:
    views = _reader_views()
    assert views == {"current_measurement", "current_function_complexity", "freshness"}
    for data in DATASETS.values():
        if data["sql"]:
            read = _tables_read(data["sql"])
            assert read, data["table_name"]
            assert read <= views, (data["table_name"], read - views)
            assert ";" not in data["sql"], data["table_name"]
        else:
            assert data["table_name"] in views
        assert data["schema"] == "public"


def test_the_table_check_sees_through_ctes() -> None:
    sql = (
        "WITH a AS (SELECT * FROM freshness), b AS MATERIALIZED (SELECT * FROM a),"
        " c AS NOT MATERIALIZED (SELECT * FROM b) SELECT * FROM c JOIN repo r"
    )
    assert _tables_read(sql) == {"freshness", "repo"}


def _measures_named(expression: str) -> set[str]:
    named: set[str] = set()
    for single in re.findall(r"measure\s*=\s*'(\w+)'", expression):
        named.add(single)
    for group in re.findall(r"measure\s+IN\s*\(([^)]*)\)", expression):
        named.update(re.findall(r"'(\w+)'", group))
    return named


def _dataset(name: str) -> dict[str, Any]:
    data: dict[str, Any] = FILES[f"datasets/code-vitals/{name}.yaml"]
    return data


@pytest.mark.parametrize(
    ("dataset", "kind"),
    [
        ("commit_snapshot", Kind.SNAPSHOT),
        ("iteration_snapshot", Kind.SNAPSHOT),
        ("current_measurement", Kind.FLOW),
    ],
)
def test_metrics_aggregate_measures_by_their_kind(dataset: str, kind: Kind) -> None:
    # Snapshot measures are read at the end of a period (the snapshot
    # datasets), flow measures summed over it (the raw view).
    for m in _dataset(dataset)["metrics"]:
        named = _measures_named(m["expression"])
        assert named, m["metric_name"]
        assert named <= KINDS.keys(), m["metric_name"]
        assert {KINDS[n] for n in named} == {kind}, m["metric_name"]


def test_snapshot_datasets_agree_on_their_metrics() -> None:
    assert _dataset("commit_snapshot")["metrics"] == _dataset("iteration_snapshot")["metrics"]
    per_commit = {c["column_name"]: c for c in _dataset("commit_snapshot")["columns"]}
    per_iteration = {c["column_name"]: c for c in _dataset("iteration_snapshot")["columns"]}
    for name in ["repo", "iteration", "measure", "unit", "area", "facet", "value"]:
        assert per_commit[name] == per_iteration[name], name


def test_ratios_are_metrics_not_measures() -> None:
    metrics = {m["metric_name"]: m for m in _dataset("commit_snapshot")["metrics"]}
    assert _measures_named(metrics["complexity_per_1000_lines"]["expression"]) == {
        "complexity_total",
        "nloc",
    }
    assert _measures_named(metrics["average_complexity"]["expression"]) == {
        "complexity_total",
        "functions",
    }
    assert "area = 'tests'" in metrics["test_share"]["expression"]
    assert not {"complexity_per_1000_lines", "average_complexity", "test_share"} & KINDS.keys()


def _columns(dataset: dict[str, Any]) -> set[str]:
    return {c["column_name"] for c in dataset["columns"]}


def _chart_columns(params: dict[str, Any]) -> set[str]:
    used = set(params.get("groupby", [])) | set(params.get("all_columns", []))
    if isinstance(params.get("x_axis"), str):
        used.add(params["x_axis"])
    used.update(f["subject"] for f in params.get("adhoc_filters", []))
    used.update(json.loads(o)[0] for o in params.get("order_by_cols", []))
    return used


def test_charts_use_known_datasets_metrics_and_columns() -> None:
    for data in CHARTS.values():
        name = data["slice_name"]
        assert data["dataset_uuid"] in DATASETS, name
        dataset = DATASETS[data["dataset_uuid"]]
        params = data["params"]
        assert params["viz_type"] == data["viz_type"], name
        metrics = list(params.get("metrics", [])) + (
            [params["metric"]] if "metric" in params else []
        )
        known = {m["metric_name"] for m in dataset["metrics"]}
        assert set(metrics) <= known, (name, set(metrics) - known)
        assert _chart_columns(params) <= _columns(dataset), (name, dataset["table_name"])


def test_no_chart_filters_on_measure() -> None:
    # Every metric selects its measures in its own FILTER (WHERE ...); a chart
    # filter on `measure` would only make swapping the metric in Explore (e.g.
    # Size by area to Code lines (tokei)) return nothing.
    for data in CHARTS.values():
        subjects = {f["subject"] for f in data["params"].get("adhoc_filters", [])}
        assert "measure" not in subjects, data["slice_name"]


def test_every_dataset_is_used() -> None:
    used = {data["dataset_uuid"] for data in CHARTS.values()} | {
        target["datasetUuid"]
        for f in FILTERS.values()
        for target in f["targets"]
        if "datasetUuid" in target
    }
    assert used == set(DATASETS)
    assert "weekly_snapshot" not in {d["table_name"] for d in DATASETS.values()}


def test_every_chart_is_on_the_dashboard_once() -> None:
    assert set(PLACED) == set(CHARTS)
    placed = [i for i in POSITION.values() if isinstance(i, dict) and i.get("type") == "CHART"]
    assert len(placed) == len(CHARTS)
    assert len(set(PLACED.values())) == len(PLACED)
    for item in placed:
        assert item["meta"]["sliceName"] == CHARTS[item["meta"]["uuid"]]["slice_name"]


def test_the_layout_is_a_tree() -> None:
    assert POSITION["ROOT_ID"]["children"] == ["GRID_ID"]
    seen: set[str] = set()

    def walk(node_id: str, path: list[str]) -> None:
        assert node_id not in seen, node_id
        seen.add(node_id)
        node = POSITION[node_id]
        assert node["id"] == node_id
        if node_id != "ROOT_ID":
            assert node["parents"] == path, node_id
        width = 0
        for child in node.get("children", []):
            walk(child, [*path, node_id])
            width += POSITION[child].get("meta", {}).get("width", 0)
        if node["type"] == "ROW":
            assert 0 < width <= 12, node_id

    walk("ROOT_ID", [])
    components = {k for k, v in POSITION.items() if isinstance(v, dict) and k != "HEADER_ID"}
    assert seen == components


def test_filters_cover_repo_area_commit_time_and_iteration() -> None:
    assert FILTERS["Repo"]["filterType"] == "filter_select"
    assert FILTERS["Area"]["filterType"] == "filter_select"
    assert FILTERS["Commit time"]["filterType"] == "filter_time"
    assert "Week" not in FILTERS
    assert FILTERS["Iteration"]["filterType"] == "filter_select"
    for name, column in [("Repo", "repo"), ("Area", "area"), ("Iteration", "iteration")]:
        (target,) = FILTERS[name]["targets"]
        assert target["column"]["name"] == column


# Charts a filter skips although their dataset has its column: Hotspots'
# moment is the Commit filter's, and the test share is a ratio across areas
# (an Area filter would make it 0% or 100%).
EXCEPTIONS = {("Commit time", "Hotspots"), ("Area", "Test share of lines")}


def test_filters_reach_every_chart_that_has_their_column() -> None:
    # A filter applies to every chart in scope whose dataset has its column,
    # so it is excluded only where the column is missing, or by EXCEPTIONS.
    for name, column in [
        ("Repo", "repo"),
        ("Area", "area"),
        ("Iteration", "iteration"),
        ("Commit time", "committed_at"),
    ]:
        excluded = set(FILTERS[name]["scope"]["excluded"])
        assert FILTERS[name]["scope"]["rootPath"] == ["ROOT_ID"]
        for data in CHARTS.values():
            has_column = column in _columns(DATASETS[data["dataset_uuid"]])
            if (name, data["slice_name"]) in EXCEPTIONS:
                assert PLACED[data["uuid"]] in excluded
            else:
                assert (PLACED[data["uuid"]] in excluded) == (not has_column), (name, data)
    assert FILTERS["Repo"]["scope"]["excluded"] == []
    assert set(FILTERS["Area"]["scope"]["excluded"]) == {
        _chart_id("freshness"),
        _chart_id("tile_test_share"),
    }


def test_commit_time_filter_reaches_every_time_series() -> None:
    # The dashboard's time range replaces each chart's TEMPORAL_RANGE filter;
    # charts on the iteration axis carry one on `committed_at` so the range
    # reaches them too.
    excluded = set(FILTERS["Commit time"]["scope"]["excluded"])
    for data_uuid, chart_id in PLACED.items():
        if chart_id in excluded:
            continue
        subjects = [
            f["subject"]
            for f in CHARTS[data_uuid]["params"]["adhoc_filters"]
            if f["operator"] == "TEMPORAL_RANGE"
        ]
        assert subjects == ["committed_at"], CHARTS[data_uuid]["slice_name"]


def _over_time() -> list[dict[str, Any]]:
    """The charts whose x-axis is a time column of their dataset."""
    charts = []
    for data in CHARTS.values():
        x_axis = data["params"].get("x_axis")
        columns = {c["column_name"]: c for c in DATASETS[data["dataset_uuid"]]["columns"]}
        if isinstance(x_axis, str) and columns[x_axis]["is_dttm"]:
            charts.append(data)
    return charts


def test_over_time_charts_have_a_point_per_commit() -> None:
    # Card #549: one x-value per trunk commit, at its commit time. A time grain
    # would bucket commits; on a snapshot it would also sum several commits'
    # states into one value.
    charts = _over_time()
    assert sorted(c["external_url"].rsplit("/", 1)[1] for c in charts) == [
        "average_complexity_by_repo.yaml",
        "churn.yaml",
        "churn_by_area.yaml",
        "complexity_by_area.yaml",
        "complexity_per_1000_by_repo.yaml",
        "e2e_scenarios_by_suite.yaml",
        "functions_over_10_by_repo.yaml",
        "operations_by_kind.yaml",
        "size_by_area.yaml",
        "tile_complexity_per_1000.yaml",
        "tile_lines.yaml",
        "tile_test_share.yaml",
    ]
    for data in charts:
        params = data["params"]
        assert params["x_axis"] == "committed_at", data["slice_name"]
        assert params["time_grain_sqla"] is None, data["slice_name"]
    for data in CHARTS.values():
        assert data["params"].get("x_axis") != "week", data["slice_name"]
        assert data["params"].get("time_grain_sqla") is None, data["slice_name"]


def test_grouped_over_time_charts_drop_empty_series() -> None:
    # The metric, not a chart filter, picks the measure, so a chart's groups
    # include other measures' (tokei's languages under `facet`, unsplit rows
    # under a NULL area) with no value: those series must not be drawn.
    for data in _over_time():
        if data["params"].get("groupby"):
            assert data["params"]["show_empty_columns"] is False, data["slice_name"]


def test_grouped_over_time_charts_query_up_to_supersets_row_cap() -> None:
    # A grouped query returns a row per (commit time, group) over every
    # snapshot measure's groups (the metric picks the measure, the empty ones
    # are dropped after the query), ~24 facets per commit today: the limit is
    # Superset's cap (ROW_LIMIT, 50,000) so it is not reached for years, not
    # the 10,000 that ~420 commits would fill.
    for data in _over_time():
        if data["params"].get("groupby"):
            assert data["params"]["row_limit"] == 50000, data["slice_name"]


def test_snapshot_charts_read_the_commit_snapshot_and_step() -> None:
    # A snapshot holds until the repo's next commit: drawn as a step that
    # changes at the commit (`end`), never a slope between commits. Flow
    # charts read the raw view, one bar per commit.
    snapshot = _dataset("commit_snapshot")["uuid"]
    flow = _dataset("current_measurement")["uuid"]
    for data in _over_time():
        if data["viz_type"] == "echarts_timeseries_bar":
            assert data["dataset_uuid"] == flow, data["slice_name"]
            continue
        assert data["dataset_uuid"] == snapshot, data["slice_name"]
        if data["viz_type"] != "big_number":
            assert data["params"]["seriesType"] == "end", data["slice_name"]
        else:
            # A lag counted in points is a number of commits across repos,
            # not a period: the tiles compare nothing.
            assert "compare_lag" not in data["params"], data["slice_name"]


def test_the_snapshot_dataset_forbids_a_time_grain_in_its_description() -> None:
    dataset = _dataset("commit_snapshot")
    assert dataset["main_dttm_col"] == "committed_at"
    assert "NO time grain" in dataset["description"]


def test_commit_filter_drives_only_the_hotspot_table() -> None:
    commit = FILTERS["Commit"]
    (target,) = commit["targets"]
    assert DATASETS[target["datasetUuid"]]["table_name"] == "hotspots"
    assert target["column"]["name"] == "commit_label"
    in_scope = set(PLACED.values()) - set(commit["scope"]["excluded"])
    assert in_scope == {_chart_id("hotspots")}
    # One commit, never none, newest first by default.
    assert commit["controlValues"]["multiSelect"] is False
    assert commit["controlValues"]["enableEmptyFilter"] is True
    assert commit["controlValues"]["defaultToFirstItem"] is True
    assert commit["controlValues"]["sortAscending"] is False
    metrics = {m["metric_name"]: m for m in DATASETS[target["datasetUuid"]]["metrics"]}
    assert metrics[commit["sortMetric"]]["expression"] == "MAX(committed_at)"


def test_filter_references_resolve() -> None:
    ids = {f["id"] for f in FILTERS.values()}
    chart_ids = set(PLACED.values())
    for f in FILTERS.values():
        assert f["type"] == "NATIVE_FILTER"
        assert set(f["cascadeParentIds"]) <= ids - {f["id"]}, f["name"]
        assert set(f["scope"]["excluded"]) <= chart_ids, f["name"]
        for target in f["targets"]:
            if "datasetUuid" in target:
                dataset = DATASETS[target["datasetUuid"]]
                assert target["column"]["name"] in _columns(dataset), f["name"]


def test_colours_come_from_the_palette() -> None:
    config = ast.parse((ROOT / "superset" / "superset_config.py").read_text())
    palette = next(
        ast.literal_eval(node.value)
        for node in config.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CODE_VITALS_COLORS" for t in node.targets)
    )
    metadata = DASHBOARD["metadata"]
    assert metadata["color_scheme"] == "codeVitals"
    assert metadata["color_scheme_domain"] == palette
    assert set(metadata["label_colors"].values()) <= set(palette)
    # Colour follows the entity: each repo, and each area, its own slot.
    repos = ["v-note", "bored", "sovereign-config"]
    assert len({metadata["label_colors"][r] for r in repos}) == len(repos)
    areas = ["server", "web", "android", "backend", "shared", "libraries", "tests", "tooling"]
    assert len({metadata["label_colors"][a] for a in areas}) == len(areas)
    for data in CHARTS.values():
        if "color_scheme" in data["params"]:
            assert data["params"]["color_scheme"] == "codeVitals"


def _bundle_file(kind: str, key: str) -> str:
    return f"uuid: {key}\nexternal_url: {importer.BUNDLE_URL}{kind}/{key}.yaml\n"


def test_bundle_uuids_by_kind() -> None:
    contents = {
        "metadata.yaml": "version: 1.0.0\n",
        "databases/code-vitals.yaml": "uuid: db\n",
        "datasets/code-vitals/a.yaml": _bundle_file("datasets", "d1"),
        "charts/a.yaml": _bundle_file("charts", "c1"),
        "charts/b.yaml": _bundle_file("charts", "c2"),
        "dashboards/a.yaml": _bundle_file("dashboards", "b1"),
    }
    assert importer.bundle_uuids(contents) == {
        "dashboards": {"b1"},
        "charts": {"c1", "c2"},
        "datasets": {"d1"},
    }
    assert importer.bundle_uuids(CONTENTS)["datasets"] == set(DATASETS)


def test_the_bundle_url_is_every_files_external_url() -> None:
    for name, data in FILES.items():
        if name.split("/")[0] in importer.KINDS:
            assert data["external_url"] == importer.BUNDLE_URL + name, name


def test_only_our_imports_are_ours() -> None:
    ours = importer.BUNDLE_URL + "charts/x.yaml"
    assert importer.is_ours(True, ours)
    assert not importer.is_ours(False, ours)
    assert not importer.is_ours(None, ours)
    assert not importer.is_ours(True, None)
    assert not importer.is_ours(True, "https://github.com/someone/else/superset/dashboard/x.yaml")


def test_stale_is_what_the_bundle_dropped_of_ours() -> None:
    ours = importer.BUNDLE_URL + "datasets/code-vitals/x.yaml"
    found = [
        ("kept", True, ours),  # still in the bundle
        ("dropped", True, ours),  # an earlier bundle's, gone from this one
        ("hand-made", False, None),  # made in Superset
        ("foreign", True, "https://example.org/other.yaml"),  # another bundle's
    ]
    assert importer.stale(found, {"kept"}) == ["dropped"]
    assert importer.stale(found, {"kept", "dropped"}) == []


def test_split_stale_deletes_only_what_nothing_kept_uses() -> None:
    removed = {"dashboards:1"}  # our dashboard, dropped from the bundle
    charts = [
        ("charts:10", []),  # on no dashboard
        ("charts:11", ["dashboards:1"]),  # only on our dropped dashboard
        ("charts:12", ["dashboards:1", "dashboards:2"]),  # also on a hand-made one
    ]
    assert importer.split_stale(charts, removed) == (["charts:10", "charts:11"], ["charts:12"])
    datasets = [
        ("datasets:20", ["charts:10", "charts:11"]),  # only our deleted charts
        ("datasets:21", ["charts:12"]),  # a kept chart
        ("datasets:22", ["charts:99"]),  # a chart made in Superset
        ("datasets:23", []),
    ]
    assert importer.split_stale(datasets, removed) == (
        ["datasets:20", "datasets:23"],
        ["datasets:21", "datasets:22"],
    )
    assert removed == {
        "dashboards:1",
        "charts:10",
        "charts:11",
        "datasets:20",
        "datasets:23",
    }


def test_read_bundle_strips_the_root(tmp_path: Path) -> None:
    (tmp_path / "databases").mkdir()
    (tmp_path / "metadata.yaml").write_text("version: 1.0.0\n")
    (tmp_path / "databases" / "code-vitals.yaml").write_text("database_name: code-vitals\n")
    (tmp_path / "notes.txt").write_text("ignored")
    assert importer.read_bundle(tmp_path) == {
        "databases/code-vitals.yaml": "database_name: code-vitals\n",
        "metadata.yaml": "version: 1.0.0\n",
    }


def test_read_bundle_needs_metadata(tmp_path: Path) -> None:
    (tmp_path / "databases").mkdir()
    (tmp_path / "databases" / "code-vitals.yaml").write_text("database_name: code-vitals\n")
    with pytest.raises(ValueError, match=r"no metadata\.yaml"):
        importer.read_bundle(tmp_path)


@pytest.mark.parametrize("extra", [None, "other.yaml"])
def test_read_bundle_needs_exactly_the_code_vitals_database(
    tmp_path: Path, extra: str | None
) -> None:
    (tmp_path / "metadata.yaml").write_text("version: 1.0.0\n")
    if extra:
        (tmp_path / "databases").mkdir()
        (tmp_path / "databases" / "code-vitals.yaml").write_text("database_name: code-vitals\n")
        (tmp_path / "databases" / extra).write_text("database_name: other\n")
    with pytest.raises(ValueError, match=r"exactly databases/code-vitals\.yaml"):
        importer.read_bundle(tmp_path)
