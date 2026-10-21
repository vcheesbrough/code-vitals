"""Fixtures for tests that need a real Postgres.

CODE_VITALS_TEST_DATABASE_URL names a server where the test may create
databases (scripts/with-test-postgres.sh starts a throwaway one). Each test
gets a fresh database, dropped afterwards. Without the variable those tests
skip — unless CODE_VITALS_REQUIRE_DB=1 (set by the image build), where a
missing server fails them, so CI can never pass by skipping.
"""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import conninfo, sql

ADMIN_URL_ENV = "CODE_VITALS_TEST_DATABASE_URL"
TEST_PASSWORDS = {
    "code_vitals_writer": "writer-test-password",
    "code_vitals_reader": "reader-test-password",
}


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get(ADMIN_URL_ENV, "")
    if url:
        return url
    if os.environ.get("CODE_VITALS_REQUIRE_DB") == "1":
        pytest.fail(f"{ADMIN_URL_ENV} is not set but CODE_VITALS_REQUIRE_DB=1")
    pytest.skip(f"{ADMIN_URL_ENV} not set (run under scripts/with-test-postgres.sh)")


@pytest.fixture
def fresh_db(admin_url: str) -> Iterator[str]:
    """The owner's URL for a new, empty database."""
    name = f"cv_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield conninfo.make_conninfo(admin_url, dbname=name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def role_url(owner_url: str, role: str) -> str:
    return conninfo.make_conninfo(owner_url, user=role, password=TEST_PASSWORDS[role])
