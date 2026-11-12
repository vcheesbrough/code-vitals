"""The dashboard import, tested against Superset itself (card #548).

tests/test_superset_dashboard.py checks the bundle and the importer's pure
functions; this checks the Superset glue: the real Superset of the
code-vitals-superset image, its metadata database and the `code-vitals`
connection, on a throwaway Postgres. scripts/check-superset-import.sh runs it
in the image the push pipeline is about to publish, so a broken import fails
the push, not a deploy. It is not shipped in the image.

The server it is given (CODE_VITALS_TEST_DATABASE_URL, the owner of a
`code_vitals` database already migrated by the collector, whose reader logs in
with CODE_VITALS_TEST_READER_PASSWORD) gets Superset's own role and database,
as scripts/deploy-stack.sh creates them. Then the deploy's bootstrap
(superset/bootstrap.sh) runs as it does on a deploy, and each test below starts
from its result: the bundle imported, through `python -m code_vitals_dashboard`.

    python dashboard_import_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

BUNDLE = Path("/app/code-vitals-dashboard")
BOOTSTRAP = "/app/docker/code-vitals-bootstrap.sh"
ADMIN_URL = os.environ["CODE_VITALS_TEST_DATABASE_URL"]
READER_PASSWORD = os.environ["CODE_VITALS_TEST_READER_PASSWORD"]
_server = urlsplit(ADMIN_URL)
HOST = _server.hostname or ""
PORT = _server.port or 5432
READER_URI = (
    f"postgresql+psycopg2://code_vitals_reader:{READER_PASSWORD}@{HOST}:{PORT}"
    f"{_server.path or '/code_vitals'}"
)
# What superset_config.py needs, as a dev deploy sets it. Superset's metadata
# database is `superset` on the same server, as in a deployment. All of these
# are test values on a server that lives for one run.
os.environ.update(
    CODE_VITALS_ENV="dev",
    SUPERSET_SECRET_KEY="code-vitals-import-test-secret-key",
    SUPERSET_DB_PASSWORD="superset-test-password",
    AUTHENTIK_CLIENT_SECRET="unused-by-the-import-test",
    CODE_VITALS_DB_HOST=HOST,
)

import code_vitals_dashboard as importer  # noqa: E402  (after the environment)

CONTENTS = importer.read_bundle(BUNDLE)
FILES = {name: yaml.safe_load(text) for name, text in CONTENTS.items()}
DATABASE_UUID = str(FILES[importer.DATABASE_FILE]["uuid"])
DASHBOARD_FILE = next(name for name in CONTENTS if name.startswith("dashboards/"))
# The dataset whose removal is tested, with the chart that uses it: one no
# dashboard filter targets, so the bundle without it is still valid.
DROPPED_DATASET = "iteration_snapshot"


def _uuid_of(kind: str, field: str, value: str) -> str:
    return next(
        str(data["uuid"])
        for name, data in FILES.items()
        if name.startswith(kind + "/") and data[field] == value
    )


DROPPED_DATASET_UUID = _uuid_of("datasets", "table_name", DROPPED_DATASET)
DROPPED_CHARTS = sorted(
    str(data["uuid"])
    for name, data in FILES.items()
    if name.startswith("charts/") and str(data["dataset_uuid"]) == DROPPED_DATASET_UUID
)


# --- bundle variants --------------------------------------------------------


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _off_the_dashboard(contents: dict[str, str], chart_uuids: list[str]) -> dict[str, str]:
    """`contents` with these charts taken off the dashboard's layout (their
    files stay)."""
    dashboard = yaml.safe_load(contents[DASHBOARD_FILE])
    position = dashboard["position"]
    for key in [
        key
        for key, item in position.items()
        if isinstance(item, dict) and str(item.get("meta", {}).get("uuid")) in chart_uuids
    ]:
        for parent in position[key]["parents"]:
            children = position[parent]["children"]
            if key in children:
                children.remove(key)
        del position[key]
    return {**contents, DASHBOARD_FILE: _dump(dashboard)}


def _without_dataset(contents: dict[str, str], dataset_uuid: str) -> dict[str, str]:
    """`contents` without a dataset and the charts that use it — what a later
    bundle that dropped them looks like."""
    dashboard = yaml.safe_load(contents[DASHBOARD_FILE])
    for native in dashboard["metadata"]["native_filter_configuration"]:
        for target in native.get("targets", []):
            assert str(target.get("datasetUuid")) != dataset_uuid, native["name"]
    charts = [
        name
        for name, text in contents.items()
        if name.startswith("charts/") and str(yaml.safe_load(text)["dataset_uuid"]) == dataset_uuid
    ]
    dataset = next(
        name
        for name, text in contents.items()
        if name.startswith("datasets/") and str(yaml.safe_load(text)["uuid"]) == dataset_uuid
    )
    chart_uuids = [str(yaml.safe_load(contents[name])["uuid"]) for name in charts]
    smaller = _off_the_dashboard(contents, chart_uuids)
    return {name: text for name, text in smaller.items() if name not in {dataset, *charts}}


def _written(contents: dict[str, str]) -> Path:
    """`contents` as a bundle directory, for the command line."""
    root = Path(tempfile.mkdtemp(prefix="bundle-"))
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# --- Superset ----------------------------------------------------------------


def _command(*args: str) -> subprocess.CompletedProcess[str]:
    """`python -m code_vitals_dashboard <args>` in its own process, as the
    bootstrap and the deploy smoke run it."""
    from superset import db

    db.session.commit()  # no open transaction while another process writes
    done = subprocess.run(
        [sys.executable, "-m", "code_vitals_dashboard", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    db.session.expire_all()
    return done


def _import(contents: dict[str, str]) -> dict[str, Any]:
    from superset import db

    db.session.expire_all()
    result: dict[str, Any] = importer.import_bundle(contents)
    db.session.expire_all()
    return result


def _connection() -> Any:
    from superset.models.core import Database

    from superset import db

    return db.session.query(Database).filter_by(database_name=importer.DATABASE_NAME).one()


def _by_uuid(model: Any, uuid_: str) -> Any:
    from superset import db

    return db.session.query(model).filter_by(uuid=uuid.UUID(uuid_)).one_or_none()


def _on_dashboard() -> set[str]:
    """The UUIDs of the charts on the bundle's dashboard, from the table."""
    from sqlalchemy.sql import select
    from superset.models.dashboard import Dashboard, dashboard_slices
    from superset.models.slice import Slice

    from superset import db

    dashboard = _by_uuid(Dashboard, str(FILES[DASHBOARD_FILE]["uuid"]))
    query = (
        select(Slice.uuid)
        .join(dashboard_slices, dashboard_slices.c.slice_id == Slice.id)
        .where(dashboard_slices.c.dashboard_id == dashboard.id)
    )
    return {str(u) for u in db.session.execute(query).scalars()}


def _permissions() -> set[str]:
    """Every permission's view-menu name: a dataset's access permission is
    named after it."""
    from superset import db, security_manager

    view = security_manager.viewmenu_model
    return {name for (name,) in db.session.query(view.name)}


def _state() -> dict[str, Any]:
    """What an import writes, ids included: datasets with their columns and
    metrics, charts, dashboards, the dashboard's charts, the connection and
    the permissions."""
    from sqlalchemy.sql import select
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard, dashboard_slices
    from superset.models.slice import Slice

    from superset import db

    db.session.expire_all()
    database = _connection()
    datasets = {
        str(t.uuid): (
            t.id,
            t.table_name,
            t.sql,
            t.description,
            t.database_id,
            t.external_url,
            sorted((c.id, c.column_name, c.expression, c.is_dttm) for c in t.columns),
            sorted((m.id, m.metric_name, m.expression) for m in t.metrics),
        )
        for t in db.session.query(SqlaTable)
    }
    charts = {
        str(s.uuid): (s.id, s.slice_name, s.viz_type, s.params, s.datasource_id, s.external_url)
        for s in db.session.query(Slice)
    }
    dashboards = {
        str(d.uuid): (d.id, d.slug, d.position_json, d.json_metadata, d.external_url)
        for d in db.session.query(Dashboard)
    }
    links = sorted(
        tuple(row)
        for row in db.session.execute(
            select(dashboard_slices.c.dashboard_id, dashboard_slices.c.slice_id)
        )
    )
    return {
        "connection": (database.id, str(database.uuid), database.sqlalchemy_uri_decrypted),
        "datasets": datasets,
        "charts": charts,
        "dashboards": dashboards,
        "links": links,
        "permissions": sorted(_permissions()),
    }


# --- the tests ---------------------------------------------------------------


def test_the_bootstrap_imports_the_bundle_onto_the_connection() -> None:
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.slice import Slice

    database = _connection()
    assert str(database.uuid) == DATABASE_UUID, "the connection keeps a UUID not the bundle's"
    assert database.sqlalchemy_uri_decrypted == READER_URI, "the import changed the URI"
    for name, data in FILES.items():
        if name.startswith("datasets/"):
            dataset = _by_uuid(SqlaTable, str(data["uuid"]))
            assert dataset is not None, f"{name} was not imported"
            assert dataset.database_id == database.id, f"{name} is not on the connection"
        elif name.startswith("charts/"):
            assert _by_uuid(Slice, str(data["uuid"])) is not None, f"{name} was not imported"
    charts = {str(d["uuid"]) for n, d in FILES.items() if n.startswith("charts/")}
    assert _on_dashboard() == charts, "the dashboard does not hold exactly the bundle's charts"


def test_the_check_reads_every_dataset_through_the_connection() -> None:
    done = _command("--check", str(BUNDLE))
    assert done.returncode == 0, done.stdout + done.stderr
    tables = sorted(d["table_name"] for n, d in FILES.items() if n.startswith("datasets/"))
    read = done.stdout.strip().splitlines()[-1]
    assert read.startswith("dashboard datasets read: "), read
    # "dashboard datasets read: <table> <rows>, …", maybe followed by "; …".
    datasets = read.removeprefix("dashboard datasets read: ").split("; ")[0]
    assert sorted(part.split()[0] for part in datasets.split(", ")) == tables, read


def test_an_existing_connection_is_given_the_bundles_uuid_and_keeps_its_uri() -> None:
    from superset.models.core import Database

    from superset import db

    database = _connection()
    database_id, uri = database.id, database.sqlalchemy_uri_decrypted
    database.uuid = uuid.uuid4()
    db.session.commit()
    connections = db.session.query(Database).count()
    _import(CONTENTS)
    database = _connection()
    assert database.id == database_id, "the connection was replaced, not aligned"
    assert str(database.uuid) == DATABASE_UUID, "the connection was not aligned"
    assert database.sqlalchemy_uri_decrypted == uri == READER_URI, "the import changed the URI"
    assert db.session.query(Database).count() == connections, "the import added a connection"


def test_a_rerun_changes_nothing() -> None:
    before = _state()
    done = _command(str(BUNDLE))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "removed (no longer in the bundle): nothing" in done.stdout, done.stdout
    after = _state()
    for key in before:
        assert after[key] == before[key], f"a second import changed the {key}"


def test_a_chart_taken_off_the_dashboard_leaves_it() -> None:
    from superset.models.slice import Slice

    chart = DROPPED_CHARTS[0]
    assert chart in _on_dashboard()
    result = _import(_off_the_dashboard(CONTENTS, [chart]))
    assert chart not in _on_dashboard(), "the dashboard's charts were merged, not replaced"
    assert _by_uuid(Slice, chart) is not None, "a chart still in the bundle was deleted"
    assert result["removed"] == [], result
    _import(CONTENTS)
    assert chart in _on_dashboard(), "the chart did not come back"


def test_a_failing_import_changes_nothing() -> None:
    from superset.connectors.sqla.models import SqlaTable

    from superset import db

    # A misaligned connection, an edited dataset and a new one, then a chart
    # on a dataset the bundle lacks: the import fails after writing the rest.
    database = _connection()
    stray = uuid.uuid4()
    database.uuid = stray
    db.session.commit()
    before = _state()
    broken = dict(CONTENTS)
    name = f"datasets/code-vitals/{DROPPED_DATASET}.yaml"
    edited = yaml.safe_load(broken[name])
    edited["description"] = "edited by a failing import"
    broken[name] = _dump(edited)
    added = dict(edited, uuid=str(uuid.uuid4()), table_name="added_by_a_failing_import")
    broken["datasets/code-vitals/added_by_a_failing_import.yaml"] = _dump(added)
    chart_name = next(n for n in broken if n.startswith("charts/"))
    chart = yaml.safe_load(broken[chart_name])
    chart["dataset_uuid"] = str(uuid.uuid4())
    broken[chart_name] = _dump(chart)
    try:
        _import(broken)
    except ValueError as error:
        assert "not in the bundle" in str(error), error
    else:
        raise AssertionError("an import of a chart without its dataset succeeded")
    assert _state() == before, "a failing import left changes behind"
    assert str(_connection().uuid) == str(stray), "a failing import kept the alignment"
    assert db.session.query(SqlaTable).filter_by(table_name=added["table_name"]).count() == 0
    _import(CONTENTS)


def test_what_the_bundle_dropped_is_deleted_with_its_columns_and_permission() -> None:
    from superset.connectors.sqla.models import SqlaTable, SqlMetric, TableColumn
    from superset.models.slice import Slice

    from superset import db

    dataset = _by_uuid(SqlaTable, DROPPED_DATASET_UUID)
    dataset_id, perm = dataset.id, dataset.perm
    assert db.session.query(TableColumn).filter_by(table_id=dataset_id).count() > 0
    assert perm in _permissions(), f"no permission {perm} to test the removal of"
    charts = {str(_by_uuid(Slice, u).slice_name) for u in DROPPED_CHARTS}
    smaller = _without_dataset(CONTENTS, DROPPED_DATASET_UUID)
    result = _import(smaller)
    assert sorted(result["removed"]) == sorted(
        [f"SqlaTable {DROPPED_DATASET}", *(f"Slice {c}" for c in charts)]
    ), result
    assert result["kept"] == [], result
    assert _by_uuid(SqlaTable, DROPPED_DATASET_UUID) is None, "the dropped dataset is still there"
    for chart in DROPPED_CHARTS:
        assert _by_uuid(Slice, chart) is None, "the dropped chart is still there"
        assert chart not in _on_dashboard()
    assert db.session.query(TableColumn).filter_by(table_id=dataset_id).count() == 0
    assert db.session.query(SqlMetric).filter_by(table_id=dataset_id).count() == 0
    assert perm not in _permissions(), f"the dropped dataset's permission {perm} is still there"
    done = _command("--check", str(_written(smaller)))
    assert done.returncode == 0, done.stdout + done.stderr
    _import(CONTENTS)
    assert _by_uuid(SqlaTable, DROPPED_DATASET_UUID) is not None, "the dataset did not come back"


def test_a_dropped_chart_on_a_dashboard_made_in_superset_is_kept_until_that_goes() -> None:
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset import db

    theirs = Dashboard(dashboard_title="Made in Superset", slug="made-in-superset")
    theirs.slices = [_by_uuid(Slice, u) for u in DROPPED_CHARTS]
    db.session.add(theirs)
    db.session.commit()
    names = [f"Slice {s.slice_name}" for s in theirs.slices]
    smaller = _without_dataset(CONTENTS, DROPPED_DATASET_UUID)
    bundle = str(_written(smaller))

    result = _import(smaller)
    assert result["removed"] == [], result
    assert sorted(result["kept"]) == sorted([*names, f"SqlaTable {DROPPED_DATASET}"]), result
    for chart in DROPPED_CHARTS:
        assert _by_uuid(Slice, chart) is not None, "a chart in use was deleted"
    assert _by_uuid(SqlaTable, DROPPED_DATASET_UUID) is not None, "a dataset in use was deleted"
    assert db.session.query(Dashboard).filter_by(slug="made-in-superset").count() == 1
    done = _command("--check", bundle)
    assert done.returncode == 0, "--check failed on a kept leftover:\n" + done.stderr

    db.session.delete(db.session.query(Dashboard).filter_by(slug="made-in-superset").one())
    db.session.commit()
    try:
        importer.check_datasets(smaller)
    except RuntimeError as error:
        assert DROPPED_DATASET in str(error), error
    else:
        raise AssertionError("check_datasets passed with a removable leftover")
    done = _command("--check", bundle)
    assert done.returncode != 0, "--check passed with a removable leftover"
    assert "no longer in the bundle but still in Superset" in done.stderr, done.stderr

    result = _import(smaller)
    assert f"SqlaTable {DROPPED_DATASET}" in result["removed"], result
    assert _by_uuid(SqlaTable, DROPPED_DATASET_UUID) is None
    importer.check_datasets(smaller)
    _import(CONTENTS)


def test_the_command_line_refuses_a_wrong_call() -> None:
    assert _command().returncode == 2
    assert _command("--check").returncode == 2


# Every test_ function, in the order defined: each starts from the bundle
# imported and leaves it so.
TESTS: list[Callable[[], None]] = [
    f for name, f in list(globals().items()) if name.startswith("test_") and callable(f)
]


def _setup() -> None:
    """Superset's role and database, as scripts/deploy-stack.sh creates them,
    then the deploy's bootstrap."""
    import psycopg2

    connection = psycopg2.connect(ADMIN_URL)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE ROLE superset LOGIN PASSWORD %s", (os.environ["SUPERSET_DB_PASSWORD"],)
        )
        cursor.execute("CREATE DATABASE superset OWNER superset")
    connection.close()
    started = time.monotonic()
    done = subprocess.run(
        [BOOTSTRAP],
        env={**os.environ, "CODE_VITALS_READER_URI": READER_URI},
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print(done.stdout[-5000:] + done.stderr[-5000:], file=sys.stderr)
        raise SystemExit(f"FAIL - the bootstrap exited {done.returncode}")
    lines = [line for line in done.stdout.splitlines() if line.startswith("dashboard imported")]
    print(f"ok   - bootstrap ({time.monotonic() - started:.0f} s): {' '.join(lines)}")


def main() -> int:
    _setup()
    from superset.app import create_app

    failures = 0
    with create_app().app_context():
        for test in TESTS:
            started = time.monotonic()
            try:
                test()
            except Exception:
                failures += 1
                print(f"FAIL - {test.__name__}", file=sys.stderr)
                traceback.print_exc()
                from superset import db

                db.session.rollback()
            else:
                print(f"ok   - {test.__name__} ({time.monotonic() - started:.1f} s)")
    print(f"{len(TESTS) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
