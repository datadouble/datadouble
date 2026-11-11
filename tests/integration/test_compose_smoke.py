# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Smoke test for the default Docker Compose group."""

import os

import psycopg
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_dsn() -> str:
    dsn = os.environ.get("DATADOUBLE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.fail("DATADOUBLE_TEST_POSTGRES_DSN is not set; run `make test-integration`")
    return dsn


def test_postgres_service_answers(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute("select current_setting('server_version_num')::int").fetchone()

    assert row is not None
    assert row[0] >= 180000
