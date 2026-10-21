"""Apply the schema migrations, then the role grants, to a code-vitals database.

Migrations are the numbered `migrations/NNNN_*.sql` files, applied in order,
each in its own transaction and recorded in `schema_migrations`, so a re-run
applies nothing. A session-level advisory lock serialises concurrent runs.
`roles.sql` is applied after them on every run (see its header), and the writer
and reader roles get their LOGIN and password when one is supplied.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable

import psycopg
from psycopg import sql

# Any constant unique to this purpose; it only has to differ from other
# advisory locks taken on the same database.
MIGRATE_LOCK_KEY = 0x0C0DE_517A15
LOGIN_ROLES = ("code_vitals_writer", "code_vitals_reader")
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    sql: str


def bundled_migrations() -> list[Migration]:
    root = resources.files("code_vitals.db") / "migrations"
    return load_migrations(root)


def load_migrations(root: Traversable) -> list[Migration]:
    migrations = []
    for entry in root.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            if entry.name.endswith(".sql"):
                raise ValueError(f"migration file {entry.name} is not named NNNN_name.sql")
            continue
        migrations.append(
            Migration(number=int(match[1]), name=entry.name, sql=entry.read_text("utf-8"))
        )
    migrations.sort(key=lambda m: m.number)
    numbers = [m.number for m in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"migrations must be numbered 1..n without gaps, found {numbers}")
    return migrations


def roles_sql() -> str:
    return (resources.files("code_vitals.db") / "roles.sql").read_text("utf-8")


def migrate(
    conninfo: str,
    role_passwords: Mapping[str, str] | None = None,
    migrations: list[Migration] | None = None,
) -> list[str]:
    """Bring the database up to date; returns the names of the migrations applied."""
    migrations = bundled_migrations() if migrations is None else migrations
    role_passwords = role_passwords or {}
    unknown = set(role_passwords) - set(LOGIN_ROLES)
    if unknown:
        raise ValueError(f"no such login role: {sorted(unknown)}")

    applied = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " number integer PRIMARY KEY,"
                " name text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT number FROM schema_migrations")}
            for migration in migrations:
                if migration.number in done:
                    continue
                with conn.transaction():
                    conn.execute(migration.sql.encode())
                    conn.execute(
                        "INSERT INTO schema_migrations (number, name) VALUES (%s, %s)",
                        (migration.number, migration.name),
                    )
                applied.append(migration.name)
            with conn.transaction():
                conn.execute(roles_sql().encode())
            for role, password in role_passwords.items():
                _set_login_password(conn, role, password)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK_KEY,))
    return applied


def _set_login_password(
    conn: psycopg.Connection[tuple[object, ...]], role: str, password: str
) -> None:
    if not password:
        raise ValueError(f"empty password for {role}")
    # ALTER ROLE takes no bind parameters, so the password is a literal in the
    # statement text. Keep it out of the server log for this session (the
    # owner is a superuser, so it may change these), as bored does for its
    # pgweb role.
    with conn.transaction():
        conn.execute("SET LOCAL log_statement = 'none'")
        conn.execute("SET LOCAL log_min_error_statement = 'panic'")
        conn.execute("SET LOCAL log_min_duration_statement = -1")
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
