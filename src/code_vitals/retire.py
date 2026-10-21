"""Retire a measure version this build no longer records.

A retired version leaves the current_* views (which read `complete` versions
only) and is never completed again. Its rows stay, as the record of what it
measured, unless they are deleted too — which needs the owner's connection, as
the writer role may not delete.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg import sql

from code_vitals import obs
from code_vitals.measures import Measure, registered


class RetireRefused(ValueError):
    pass


@dataclass(frozen=True)
class Retired:
    previous_status: str
    rows_deleted: int


def retire(
    conninfo: str,
    name: str,
    version: int,
    *,
    delete_rows: bool = False,
    measures: Sequence[Measure] | None = None,
) -> Retired:
    measures = registered() if measures is None else list(measures)
    if any((m.name, m.version) == (name, version) for m in measures):
        raise RetireRefused(
            f"{name} v{version} is registered by this build; retire it once a build no"
            " longer records it"
        )
    with psycopg.connect(conninfo) as conn, conn.transaction():
        row = conn.execute(
            "SELECT id, status::text FROM measure WHERE name = %s AND version = %s FOR UPDATE",
            (name, version),
        ).fetchone()
        if row is None:
            raise RetireRefused(f"no measure {name} v{version} is recorded")
        measure_id, previous = row
        with obs.db_span("UPDATE", "retire measure"):
            conn.execute("UPDATE measure SET status = 'retired' WHERE id = %s", (measure_id,))
        deleted = 0
        if delete_rows:
            for table in ("measurement", "function_complexity", "commit_measure"):
                with obs.db_span("DELETE", f"delete {table} rows"):
                    deleted += conn.execute(
                        sql.SQL("DELETE FROM {} WHERE measure_id = %s").format(
                            sql.Identifier(table)
                        ),
                        (measure_id,),
                    ).rowcount
    return Retired(str(previous), deleted)
