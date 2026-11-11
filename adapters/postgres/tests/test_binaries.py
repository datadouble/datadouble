# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Finding and running the PostgreSQL programs, with stand-in executables."""

from pathlib import Path

import psycopg
import pytest

from datadouble.engine import EngineError
from datadouble.registry import installed
from datadouble_postgres import PostgresEngine
from datadouble_postgres.cluster import Binaries, Cluster, find_binaries
from datadouble_postgres.errors import database_error


def fake_bindir(root: Path, *, version: str = "postgres (PostgreSQL) 18.6 (Debian)") -> Path:
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    scripts = {
        "initdb": "exit 0",
        "pg_dump": "exit 0",
        "postgres": f"echo '{version}'",
        "fails": "echo 'ERROR:  bad \"secret\" value' >&2; echo 'DETAIL:  Key (x)=(1)' >&2; exit 3",
    }
    for name, body in scripts.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    return bindir


def test_configured_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = fake_bindir(tmp_path)
    monkeypatch.setenv("DATADOUBLE_POSTGRES_BINDIR", str(bindir))
    assert find_binaries().bindir == bindir


def test_newest_installed_version_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATADOUBLE_POSTGRES_BINDIR", raising=False)
    fake_bindir(tmp_path / "16")
    newest = fake_bindir(tmp_path / "18")
    fake_bindir(tmp_path / "devel")
    assert find_binaries(tmp_path).bindir == newest


def test_binaries_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATADOUBLE_POSTGRES_BINDIR", raising=False)
    bindir = fake_bindir(tmp_path / "elsewhere")
    monkeypatch.setenv("PATH", str(bindir))
    assert find_binaries(tmp_path / "nothing").bindir == bindir


def test_no_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATADOUBLE_POSTGRES_BINDIR", str(tmp_path))
    with pytest.raises(EngineError, match="DATADOUBLE_POSTGRES_BINDIR"):
        find_binaries()
    monkeypatch.delenv("DATADOUBLE_POSTGRES_BINDIR")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(EngineError, match="were not found"):
        find_binaries(tmp_path / "nothing")


def test_versions_and_failures(tmp_path: Path) -> None:
    binaries = Binaries(fake_bindir(tmp_path))
    assert binaries.version() == "18.6"
    with pytest.raises(EngineError) as caught:
        binaries.run("fails", [])
    assert str(caught.value) == 'fails exited with status 3: ERROR:  bad "…" value'
    odd = Binaries(fake_bindir(tmp_path / "odd", version="something else"))
    with pytest.raises(EngineError, match="cannot read the PostgreSQL version"):
        odd.version()


def test_a_stopped_cluster_has_no_socket(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="not running"):
        _ = Cluster(Binaries(tmp_path), tmp_path / "data").socket_dir


def test_database_errors_without_details() -> None:
    error = database_error(psycopg.errors.UniqueViolation(), "loading")
    assert str(error) == "loading failed: UniqueViolation"


def test_the_adapter_is_installed() -> None:
    engine = installed().engine("postgres")
    assert isinstance(engine, PostgresEngine)
    assert engine.name == "postgres"
    assert engine.capabilities().nested
