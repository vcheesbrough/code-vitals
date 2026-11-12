"""Import the code-vitals dashboard bundle into Superset (card #500).

The dashboard is code: superset/dashboard/ holds it as Superset's
import/export bundle (metadata.yaml, databases/, datasets/, charts/,
dashboards/ — an export zip, unzipped), copied into the image at
/app/code-vitals-dashboard. superset/bootstrap.sh runs this on every deploy,
after `set-database-uri`:

    python -m code_vitals_dashboard /app/code-vitals-dashboard

It overwrites the bundle's datasets, charts and dashboard (matched by UUID),
so a deleted or edited dashboard is restored by the next deploy. It never
imports the database file: the connection belongs to `set-database-uri`,
which alone knows the reader's password. The database file only names that
connection; the import gives the existing connection the file's UUID (the
datasets refer to it by UUID) and leaves its URI alone.

The import runs as no user, with Superset's permission checks off: there is no
local admin (everyone signs in through Authentik), and this is the deployment
itself. Only this module and bootstrap.sh touch Superset's internals here;
scripts/check-superset-image.sh fails the build if an upgrade removes them.

`--check` counts every dataset through the connection instead of importing:
scripts/smoke-superset.sh runs it in the deployed Superset container.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

DATABASE_NAME = "code-vitals"
DATABASE_FILE = "databases/code-vitals.yaml"


def read_bundle(root: Path) -> dict[str, str]:
    """Every YAML file under `root`, keyed by its path relative to it — the
    contents of an export zip once its top directory is stripped."""
    contents = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.yaml"))
    }
    if "metadata.yaml" not in contents:
        raise ValueError(f"{root} is not a Superset bundle: no metadata.yaml")
    databases = sorted(name for name in contents if name.startswith("databases/"))
    if databases != [DATABASE_FILE]:
        raise ValueError(f"the bundle must declare exactly {DATABASE_FILE}, found {databases}")
    return contents


def import_bundle(contents: dict[str, str]) -> dict[str, int]:
    """Import (overwrite) the bundle's datasets, charts and dashboards against
    the existing `code-vitals` connection; returns how many of each. Needs an
    app context. All or nothing: any failure rolls the whole import back."""
    # Superset is importable only inside its image; keep this module loadable
    # without it (tests/test_superset_dashboard.py).
    import yaml
    from sqlalchemy.sql import delete, insert
    from superset.charts.schemas import ImportV1ChartSchema
    from superset.commands.chart.importers.v1.utils import import_chart
    from superset.commands.dashboard.importers.v1.utils import (
        find_chart_uuids,
        import_dashboard,
        update_id_refs,
    )
    from superset.commands.dataset.importers.v1.utils import import_dataset
    from superset.commands.exceptions import CommandInvalidError
    from superset.commands.importers.v1.utils import (
        load_configs,
        load_metadata,
        validate_metadata_type,
    )
    from superset.commands.utils import update_chart_config_dataset
    from superset.dashboards.schemas import ImportV1DashboardSchema
    from superset.datasets.schemas import ImportV1DatasetSchema
    from superset.models.core import Database
    from superset.models.dashboard import dashboard_slices

    from superset import db

    declared = yaml.safe_load(contents[DATABASE_FILE])
    if declared.get("database_name") != DATABASE_NAME:
        raise ValueError(f"{DATABASE_FILE} must name the {DATABASE_NAME} connection")
    database_uuid = str(declared["uuid"])

    try:
        database = db.session.query(Database).filter_by(database_name=DATABASE_NAME).one_or_none()
        if database is None:
            raise RuntimeError(
                f"no {DATABASE_NAME} connection: run `superset set-database-uri` first"
            )
        if str(database.uuid) != database_uuid:
            database.uuid = uuid.UUID(database_uuid)
            db.session.flush()

        exceptions: list[Any] = []
        validate_metadata_type(load_metadata(contents), "Dashboard", exceptions)
        configs = load_configs(
            {name: text for name, text in contents.items() if not name.startswith("databases/")},
            {
                "charts/": ImportV1ChartSchema(),
                "dashboards/": ImportV1DashboardSchema(),
                "datasets/": ImportV1DatasetSchema(),
            },
            passwords={},
            exceptions=exceptions,
            ssh_tunnel_passwords={},
            ssh_tunnel_private_keys={},
            ssh_tunnel_priv_key_passwords={},
            encrypted_extra_secrets={},
        )
        if exceptions:
            raise CommandInvalidError("the dashboard bundle is invalid", exceptions)

        dataset_info: dict[str, dict[str, Any]] = {}
        for name, config in sorted(configs.items()):
            if not name.startswith("datasets/"):
                continue
            if str(config["database_uuid"]) != database_uuid:
                raise ValueError(f"{name} must use the {DATABASE_NAME} connection")
            config["database_id"] = database.id
            dataset = import_dataset(config, overwrite=True, ignore_permissions=True)
            dataset_info[str(dataset.uuid)] = {
                "datasource_id": dataset.id,
                "datasource_type": dataset.datasource_type,
                "datasource_name": dataset.table_name,
            }

        chart_ids: dict[str, int] = {}
        for name, config in sorted(configs.items()):
            if not name.startswith("charts/"):
                continue
            dataset_uuid = str(config["dataset_uuid"])
            if dataset_uuid not in dataset_info:
                raise ValueError(f"{name} uses dataset {dataset_uuid}, which is not in the bundle")
            config = update_chart_config_dataset(config, dataset_info[dataset_uuid])
            chart = import_chart(config, overwrite=True, ignore_permissions=True)
            chart_ids[str(chart.uuid)] = chart.id

        dashboards = 0
        for name, config in sorted(configs.items()):
            if not name.startswith("dashboards/"):
                continue
            missing = find_chart_uuids(config["position"]) - chart_ids.keys()
            if missing:
                raise ValueError(f"{name} places charts not in the bundle: {sorted(missing)}")
            config = update_id_refs(config, chart_ids, dataset_info)
            dashboard = import_dashboard(config, overwrite=True, ignore_permissions=True)
            # Replace, not merge, the dashboard's charts.
            db.session.execute(
                delete(dashboard_slices).where(dashboard_slices.c.dashboard_id == dashboard.id)
            )
            db.session.execute(
                insert(dashboard_slices).values(
                    [
                        {"dashboard_id": dashboard.id, "slice_id": chart_ids[uuid]}
                        for uuid in sorted(find_chart_uuids(config["position"]))
                    ]
                )
            )
            dashboards += 1
        db.session.commit()
    except BaseException:
        db.session.rollback()
        raise
    return {"datasets": len(dataset_info), "charts": len(chart_ids), "dashboards": dashboards}


def check_datasets(contents: dict[str, str]) -> dict[str, int]:
    """Count each of the bundle's datasets through the imported `code-vitals`
    connection, as the dashboard would read it: proves the connection works
    as the reader after the import and every dataset's SQL runs. Needs an app
    context."""
    import yaml
    from superset.connectors.sqla.models import SqlaTable

    from superset import db

    counts: dict[str, int] = {}
    for name, text in sorted(contents.items()):
        if not name.startswith("datasets/"):
            continue
        declared = yaml.safe_load(text)
        dataset = db.session.query(SqlaTable).filter_by(uuid=uuid.UUID(declared["uuid"])).one()
        if dataset.database.database_name != DATABASE_NAME:
            raise RuntimeError(f"{dataset.table_name} is not on the {DATABASE_NAME} connection")
        source = f"({dataset.sql}) AS d" if dataset.sql else dataset.table_name
        frame = dataset.database.get_df(f"SELECT count(*) AS n FROM {source}")
        counts[dataset.table_name] = int(frame["n"][0])
    return counts


USAGE = "usage: python -m code_vitals_dashboard [--check] <bundle-directory>"


def main(argv: list[str]) -> int:
    args = argv[1:]
    check = args[:1] == ["--check"]
    if check:
        args = args[1:]
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    contents = read_bundle(Path(args[0]))
    from superset.app import create_app

    with create_app().app_context():
        if check:
            rows = check_datasets(contents)
            print("dashboard datasets read: " + ", ".join(f"{k} {v}" for k, v in rows.items()))
            return 0
        counts = import_bundle(contents)
    print(
        f"dashboard imported: {counts['datasets']} datasets, {counts['charts']} charts, "
        f"{counts['dashboards']} dashboard(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
