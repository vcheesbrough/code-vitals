"""The dashboard charts' row limits, checked after each collect (card #553).

The deploy smoke counts each grouped chart's rows in Superset (card #552,
tests/test_superset_row_limits.py); the collector counts the same rows after
every `collect`, from src/code_vitals/chart_rows.toml. These tests hold that
manifest equal to the bundle, check that the collector's counts are Superset's
on a real database, and that `collect` fails, naming the chart, once one is
past the headroom — with the run still recorded.
"""

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from code_vitals import cli, row_limits
from code_vitals import collect as collect_module
from code_vitals.db.migrate import migrate
from code_vitals.settings import SETTINGS_ENV

from .conftest import TEST_PASSWORDS, role_url
from .gitfixture import FixtureRepo, three_commit_history
from .test_collect import FIXTURE_SETTINGS, ONLY_FILES
from .test_superset_dashboard import CHARTS, CONTENTS, DATASETS, ROOT, importer
from .test_superset_datasets import _seed

MANIFEST = ROOT / "src" / "code_vitals" / "chart_rows.toml"


def test_the_manifest_is_rendered_from_the_bundle() -> None:
    # Any chart or dataset change that alters a grouped query re-renders it.
    assert MANIFEST.read_text(encoding="utf-8") == importer.row_limit_manifest(CONTENTS), (
        "src/code_vitals/chart_rows.toml differs from superset/dashboard/; re-render it:"
        " uv run python superset/code_vitals_dashboard.py --row-limits superset/dashboard"
        " > src/code_vitals/chart_rows.toml"
    )


def test_the_manifest_has_every_chart_the_deploy_check_counts() -> None:
    manifest = row_limits.load_manifest()
    grouped = {
        data["external_url"].rsplit("/", 1)[1]
        for data in CHARTS.values()
        if importer.grouped_by(data["params"])
    }
    assert {c.file for c in manifest.charts} == grouped
    assert "size_by_repo.yaml" in grouped and "complexity_by_repo.yaml" in grouped
    assert manifest.headroom == importer.ROW_LIMIT_HEADROOM
    by_file = {c.file: c for c in manifest.charts}
    assert by_file["size_by_repo.yaml"].limit == 50000
    assert by_file["churn_by_iteration.yaml"].limit == 10000
    # A tile sets no limit: Superset's default.
    assert by_file["tile_lines.yaml"].limit == importer.ROW_LIMIT


def test_superset_applies_the_manifests_limits() -> None:
    # superset_config.py sets the default and the cap from the module that
    # renders the manifest, so the two cannot disagree.
    config = ast.parse((ROOT / "superset" / "superset_config.py").read_text())
    assigned = {
        node.targets[0].id: ast.unparse(node.value)
        for node in config.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert assigned["ROW_LIMIT"] == "code_vitals_dashboard.ROW_LIMIT"
    assert assigned["SQL_MAX_ROW"] == "code_vitals_dashboard.SQL_MAX_ROW"
    # Superset 6.1.0's own defaults, so setting them changed nothing.
    assert (importer.ROW_LIMIT, importer.SQL_MAX_ROW) == (50000, 100000)


def test_the_manifest_command_needs_no_superset(capsys: pytest.CaptureFixture[str]) -> None:
    assert importer.main(["x", "--row-limits", str(ROOT / "superset" / "dashboard")]) == 0
    assert capsys.readouterr().out == MANIFEST.read_text(encoding="utf-8")


def test_each_dataset_is_read_once_for_all_its_charts() -> None:
    manifest = row_limits.load_manifest()
    charts = [c for c in manifest.charts if c.dataset == "commit_snapshot"]
    sql = row_limits.dataset_sql(manifest, "commit_snapshot", charts)
    assert sql.startswith("WITH d AS MATERIALIZED (\n-- Snapshot measures")
    assert sql.count("FROM current_measurement") == 1
    assert sql.count("SELECT count(*) AS n FROM (SELECT 1 FROM d") == len(charts)


def _chart(limit: int, file: str = "x.yaml") -> row_limits.Chart:
    return row_limits.Chart(file, file.removesuffix(".yaml"), "t", limit, "SELECT 1")


def test_the_report_names_every_chart_past_the_headroom_and_the_closest() -> None:
    counts = [
        row_limits.ChartRows(_chart(10000, "churn.yaml"), 8001),
        row_limits.ChartRows(_chart(50000, "operations.yaml"), 7497),
        row_limits.ChartRows(_chart(10000, "lines.yaml"), 8000),
    ]
    checked = row_limits.report(counts, 0.8)
    assert checked.problems == (
        "chart churn (churn.yaml) 8001 of 10000 rows: past 80% of its row limit, past which"
        " Superset truncates it without an error; raise its row_limit or coarsen its x-axis"
        " in superset/dashboard/",
    )
    assert checked.summary == (
        "chart rows at most 80% of a row limit (churn (churn.yaml) 8001 of 10000), fails past 80%"
    )
    assert row_limits.report([], 0.8) == row_limits.RowLimitCheck((), (), "")


@pytest.fixture
def db(fresh_db: str) -> Iterator[str]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db


def test_the_collector_counts_what_superset_would_as_the_writer(db: str) -> None:
    _seed(db)
    manifest = row_limits.load_manifest()
    counts = row_limits.count_rows(role_url(db, "code_vitals_writer"), manifest)
    by_file = {c.chart.file: c.rows for c in counts}
    assert [c.chart for c in counts] == list(manifest.charts)
    with psycopg.connect(role_url(db, "code_vitals_reader")) as conn:
        for data in CHARTS.values():
            file = data["external_url"].rsplit("/", 1)[1]
            if file not in by_file:
                continue
            dataset = DATASETS[data["dataset_uuid"]]
            source = importer.count_source(dataset["sql"], dataset["table_name"])
            sql = importer.row_count_sql(data["params"], source, {})
            assert conn.execute(sql).fetchone() == (by_file[file],), file
    # tests/test_superset_row_limits.py's seed counts: bored at five moments,
    # v-note at four.
    assert by_file["size_by_repo.yaml"] == 9


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return three_commit_history(tmp_path)


def _collect(writer: str, repo: FixtureRepo) -> int:
    status: int = cli.main(
        ["collect", "--repo", "fixture", "--clone", str(repo.path)],
        {cli.DATABASE_URL_ENV: writer, SETTINGS_ENV: FIXTURE_SETTINGS},
    )
    return status


def _runs(owner: str) -> list[tuple[object, ...]]:
    with psycopg.connect(owner) as conn:
        return conn.execute("SELECT outcome::text FROM run").fetchall()


def test_collect_reports_the_charts_headroom(
    db: str, repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    assert _collect(role_url(db, "code_vitals_writer"), repo) == 0
    out = capsys.readouterr().out
    assert "code-vitals collect: chart rows at most 0% of a row limit (" in out
    assert out.rstrip().endswith("fails past 80%")


def test_collect_fails_naming_a_chart_near_its_limit_after_recording(
    db: str,
    repo: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    real = row_limits.load_manifest()
    # Size by repo with room for only 3 of the fixture's 3 commit times' rows:
    # 3 > 80% of 3.
    tight = dataclasses.replace(
        real,
        charts=tuple(
            dataclasses.replace(c, limit=3) if c.file == "size_by_repo.yaml" else c
            for c in real.charts
        ),
    )
    monkeypatch.setattr(row_limits, "load_manifest", lambda: tight)

    assert _collect(role_url(db, "code_vitals_writer"), repo) == 1
    captured = capsys.readouterr()
    # A warning, so it reaches the step log on stderr (cli) and OTLP.
    (warning,) = [r for r in caplog.records if r.name == "code_vitals.row_limits"
                  and r.levelname == "WARNING"]  # fmt: skip
    assert warning.getMessage().startswith(
        "chart Size by repo (size_by_repo.yaml) 3 of 3 rows: past 80% of its row limit"
    )
    assert "1 dashboard chart(s) near their row limit" in captured.err
    assert "3 commit(s) measured of 3 to do" in captured.out
    # The data is recorded all the same: only the step reports the chart.
    assert _runs(db) == [("success",)]


def test_collect_fails_when_the_rows_cannot_be_counted(
    db: str, repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(collect_module, "registered", lambda: ONLY_FILES)
    broken = dataclasses.replace(row_limits.load_manifest(), datasets={})
    monkeypatch.setattr(row_limits, "load_manifest", lambda: broken)

    assert _collect(role_url(db, "code_vitals_writer"), repo) == 1
    assert "the dashboard charts' row limits could not be checked" in capsys.readouterr().err
    assert _runs(db) == [("success",)]
