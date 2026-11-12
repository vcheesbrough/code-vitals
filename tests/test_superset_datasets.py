"""The dashboard's SQL datasets (superset/dashboard/), run as the reader
against a real database: the reader may run every one of them, and the
commit snapshot carries each repo's state forward to every commit time
(card #549)."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import yaml

from code_vitals.db.migrate import migrate

from .conftest import TEST_PASSWORDS, role_url

DATASETS = Path(__file__).resolve().parent.parent / "superset" / "dashboard" / "datasets"


def _sql(name: str) -> str:
    sql: str = yaml.safe_load((DATASETS / "code-vitals" / f"{name}.yaml").read_text())["sql"]
    return sql


def _sha(n: int) -> str:
    return f"{n:040x}"


@pytest.fixture
def db(fresh_db: str) -> Iterator[str]:
    migrate(fresh_db, TEST_PASSWORDS)
    yield fresh_db


# Two repos interleaving their commits (bored at 1, 3, 3 and 5 o'clock, v-note
# at 2 and 4); two of bored's share a time. nloc is a snapshot, split by area;
# lines_added a flow, which the snapshot must leave out.
COMMITS = [
    # id, repo id, committed at, iteration, nloc by area
    (1, 1, "01:00", 1, {"server": 100, "tests": 10}),
    (2, 2, "02:00", 1, {"web": 50}),
    (3, 1, "03:00", 2, {"server": 110, "tests": 10}),
    (4, 1, "03:00", 2, {"server": 120, "tests": 20}),
    (5, 2, "04:00", 2, {"web": 40}),
    (6, 1, "05:00", 3, {"server": 130}),
]


def _seed(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO repo (id, name, trunk) VALUES (1, 'bored', 'main'), (2, 'v-note', 'main')"
        )
        conn.execute(
            "INSERT INTO measure (id, name, version, unit, kind, definition, recomputable, status)"
            " VALUES (1, 'nloc', 1, 'lines', 'snapshot', 'v1', true, 'complete'),"
            "        (2, 'lines_added', 1, 'lines', 'flow', 'v1', true, 'complete')"
        )
        conn.execute(
            "INSERT INTO area_ruleset (id, repo_id, version, rules, status)"
            " VALUES (1, 1, 1, '{}', 'complete'), (2, 2, 1, '{}', 'complete')"
        )
        conn.execute(
            "INSERT INTO area (id, ruleset_id, name)"
            " VALUES (1, 1, 'server'), (2, 1, 'tests'), (3, 2, 'web')"
        )
        areas = {"server": 1, "tests": 2, "web": 3}
        for commit_id, repo_id, at, iteration, nloc in COMMITS:
            conn.execute(
                "INSERT INTO commit (id, repo_id, sha, committed_at, author, subject, iteration)"
                " VALUES (%s, %s, %s, %s, 'a', 's', %s)",
                (commit_id, repo_id, _sha(commit_id), f"2026-10-01T{at}Z", iteration),
            )
            for area, value in nloc.items():
                conn.execute(
                    "INSERT INTO measurement (commit_id, measure_id, area_id, facet, value)"
                    " VALUES (%s, 1, %s, '', %s), (%s, 2, %s, '', 7)",
                    (commit_id, areas[area], value, commit_id, areas[area]),
                )


def _read(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(role_url(url, "code_vitals_reader")) as conn:
        return conn.execute(sql).fetchall()


@pytest.mark.parametrize("path", sorted(DATASETS.rglob("*.yaml")), ids=lambda p: p.stem)
def test_the_reader_runs_every_dataset(db: str, path: Path) -> None:
    _seed(db)
    dataset = yaml.safe_load(path.read_text())
    source = f"({dataset['sql']}) AS d" if dataset["sql"] else dataset["table_name"]
    # Runs as the reader (only the views are granted); hotspots has no rows here.
    (count,) = _read(db, f"SELECT count(*) FROM {source}")[0]
    assert isinstance(count, int)
    assert count > 0 or dataset["table_name"] == "hotspots"


def test_the_commit_snapshot_carries_each_repo_forward(db: str) -> None:
    _seed(db)
    rows = _read(
        db,
        "SELECT to_char(committed_at AT TIME ZONE 'UTC', 'HH24:MI'), repo, sha,"
        " to_char(sha_committed_at AT TIME ZONE 'UTC', 'HH24:MI'), iteration,"
        " sum(value) FILTER (WHERE measure = 'nloc'), count(*) FILTER (WHERE measure <> 'nloc')"
        f" FROM ({_sql('commit_snapshot')}) AS d GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2",
    )
    assert rows == [
        # moment, repo, the commit it is at, that commit's time, its iteration
        ("01:00", "bored", _sha(1), "01:00", 1, 110, 0),
        ("02:00", "bored", _sha(1), "01:00", 1, 110, 0),  # carried
        ("02:00", "v-note", _sha(2), "02:00", 1, 50, 0),
        # Two commits at one time: the later sha, as iteration_snapshot picks.
        ("03:00", "bored", _sha(4), "03:00", 2, 140, 0),
        ("03:00", "v-note", _sha(2), "02:00", 1, 50, 0),  # carried
        ("04:00", "bored", _sha(4), "03:00", 2, 140, 0),  # carried
        ("04:00", "v-note", _sha(5), "04:00", 2, 40, 0),
        ("05:00", "bored", _sha(6), "05:00", 3, 130, 0),
        ("05:00", "v-note", _sha(5), "04:00", 2, 40, 0),  # carried
    ]


def test_the_commit_snapshot_has_a_moment_per_commit_time_and_keeps_areas(db: str) -> None:
    _seed(db)
    sql = _sql("commit_snapshot")
    moments = _read(db, f"SELECT DISTINCT committed_at FROM ({sql}) AS d ORDER BY 1")
    times = _read(db, "SELECT DISTINCT committed_at FROM current_measurement ORDER BY 1")
    assert moments == times
    # An area a repo no longer has (bored's tests at 05:00) is simply absent:
    # the state is the repo's commit, not each area's last value.
    rows = _read(
        db,
        "SELECT to_char(committed_at AT TIME ZONE 'UTC', 'HH24:MI'), area, value"
        f" FROM ({sql}) AS d WHERE repo = 'bored' AND area = 'tests' ORDER BY 1",
    )
    assert rows == [
        ("01:00", "tests", 10),
        ("02:00", "tests", 10),
        ("03:00", "tests", 20),
        ("04:00", "tests", 20),
    ]
