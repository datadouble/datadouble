# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Running the PostgreSQL programs through a container runtime, with a scripted one."""

import socket
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest

from datadouble.engine import EngineError
from datadouble.registry import installed
from datadouble.runtime import Completed, Container, Endpoint
from datadouble_postgres import PostgresEngine
from datadouble_postgres.cluster import Cluster, Tools, default_image
from datadouble_postgres.errors import database_error


@dataclass
class ScriptedServer:
    endpoint: Endpoint
    commands: list[list[str]] = field(default_factory=list[list[str]])

    def execute(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> Completed:
        self.commands.append(list(command))
        return Completed(status=0, stdout="done", stderr="")


@dataclass
class ScriptedRuntime:
    """Answers every container with ``reply``; serves on a port nothing listens on."""

    reply: Completed
    ran: list[Container] = field(default_factory=list[Container])

    def run(self, container: Container) -> Completed:
        self.ran.append(container)
        return self.reply

    @contextmanager
    def serve(self, container: Container) -> Generator[ScriptedServer]:
        self.ran.append(container)
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = int(unused.getsockname()[1])
        yield ScriptedServer(Endpoint(host="127.0.0.1", port=port))

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def tools(tmp_path: Path, stdout: str = "", stderr: str = "", status: int = 0) -> Tools:
    runtime = ScriptedRuntime(Completed(status=status, stdout=stdout, stderr=stderr))
    return Tools(runtime, "postgres:18-trixie", tmp_path / "home")


def test_programs_run_as_the_wrapped_user(tmp_path: Path) -> None:
    made = tools(tmp_path, stdout="postgres (PostgreSQL) 18.6 (Debian)")
    assert made.version() == "18.6"
    assert isinstance(made.runtime, ScriptedRuntime)
    (container,) = made.runtime.ran
    assert container.command == ("postgres", "--version")
    assert container.image == "postgres:18-trixie"
    assert container.env["LD_PRELOAD"] == "libnss_wrapper.so"
    passwd = Path(container.env["NSS_WRAPPER_PASSWD"]).read_text("utf-8")
    assert passwd.startswith("datadouble:x:")
    assert (tmp_path / "home" / "pg_hba.conf").read_text("utf-8").startswith("local all all trust")
    assert container.mounts == (tmp_path / "home",)


def test_failures_are_redacted(tmp_path: Path) -> None:
    failing = tools(
        tmp_path,
        stderr='ERROR:  bad "secret" value\nDETAIL:  Key (x)=(1)\n',
        status=3,
    )
    with pytest.raises(EngineError) as caught:
        failing.run("pg_dump", [], tmp_path)
    assert str(caught.value) == 'pg_dump exited with status 3: ERROR:  bad "…" value'
    with pytest.raises(EngineError, match="cannot read the PostgreSQL version"):
        tools(tmp_path, stdout="something else").version()


def test_clusters(tmp_path: Path) -> None:
    made = tools(tmp_path)
    cluster = Cluster(made, tmp_path / "db" / "pgdata", tmp_path / "db", [tmp_path / "extract"])
    with pytest.raises(EngineError, match="not running"):
        _ = cluster.server
    with cluster.running():
        assert cluster.execute("pg_restore", ["x"]) == "done"
        assert cluster.socket_conninfo("shop") == "host=/tmp dbname=shop user=postgres"
        with pytest.raises(EngineError, match="connecting to the working database failed"):
            cluster.connect("shop")
    assert isinstance(made.runtime, ScriptedRuntime)
    (served,) = made.runtime.ran
    assert served.port == 5432
    assert served.mounts == (tmp_path / "home", tmp_path / "db", tmp_path / "extract")
    assert set(served.secrets) == {"DATADOUBLE_PASSWORD"}
    assert served.secrets["DATADOUBLE_PASSWORD"] not in " ".join(served.command)


def test_images() -> None:
    assert default_image("16.4") == "postgres:16.4-trixie"


def test_database_errors_without_details() -> None:
    error = database_error(psycopg.errors.UniqueViolation(), "loading")
    assert str(error) == "loading failed: UniqueViolation"


def test_the_adapter_is_installed() -> None:
    engine = installed().engine("postgres")
    assert isinstance(engine, PostgresEngine)
    assert engine.name == "postgres"
    assert engine.capabilities().nested
