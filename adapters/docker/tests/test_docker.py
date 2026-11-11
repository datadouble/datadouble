# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Docker replicate adapter, and the whole non-production side with real tools.

These tests build an image from a scratch PostgreSQL database, sign it with a
real cosign key pair, promote and fetch it, and serve copies from the
Docker-in-Docker daemon in the Compose default group.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest

import datadouble_docker
from datadouble.api import CopiesResponse, ErrorResponse, handle_json
from datadouble.catalog import CatalogLock, write_lock
from datadouble.engine import ServerPlan
from datadouble.images import ImageIdentity, ImageSummary, Verification
from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.replicate import ClaimState, Copy, CopyKind, CopyState, ReplicateError
from datadouble.spec import ReplicateMode, StrictLevel
from datadouble_docker import DockerReplicator, DockerSettings, docker_binary
from datadouble_postgres import PostgresEngine
from test_conformance import PostgresScratch, scratch

SERVER = (
    "postgres:18-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
)


@pytest.fixture
def source() -> Generator[PostgresScratch]:
    with scratch() as made:
        assert isinstance(made, PostgresScratch)
        made.add_table("people")
        made.add_column("name")
        for number in range(1, 4):
            made.insert(number)
        yield made


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COSIGN_PASSWORD", "test-only")
    cosign = shutil.which("cosign")
    assert cosign is not None
    subprocess.run([cosign, "generate-key-pair"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def write_spec(source: PostgresScratch, tmp_path: Path, keys: Path, kind: str) -> Path:
    address = os.environ.get("DATADOUBLE_TEST_DOCKER_ADDRESS", "localhost")
    spec = {
        "name": "scratch",
        "source": {"engine": "postgres", "connection": dict(source.connection)},
        "subset": {"seeds": {"items": "id > 0"}},
        "store": [{"filesystem": {"path": str(tmp_path / "build")}}],
        "signing": {
            "cosign": {"key": {"file": str(keys / "cosign.key")}, "transparency_log": False}
        },
        "trust": {
            "cosign": {"public_key": {"file": str(keys / "cosign.pub")}, "transparency_log": False}
        },
        "promote": {"to": [{"filesystem": {"path": str(tmp_path / "export")}}]},
        "fetch": {
            "from": {"filesystem": {"path": str(tmp_path / "export")}},
            "staging": str(tmp_path / "staging"),
        },
        "replicate": {
            "using": {"docker-volume": {"kind": kind, "server_image": SERVER, "host": address}},
            "mode": "on-demand",
            "size": 1,
        },
        "announce": [{"dotenv": {"path": str(tmp_path / "clone.env")}}],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with PostgresEngine().connect(source.connection) as session:
        write_lock(CatalogLock.of(session.catalog()), tmp_path / "catalog.lock")
    return path


def call(**request: object) -> object:
    response = handle_json(json.dumps(request))
    assert not isinstance(response, ErrorResponse), response
    return response


def build_and_stage(spec: Path, tmp_path: Path) -> None:
    call(
        command="run",
        spec=str(spec),
        stages=["plan", "extract", "load", "transform", "package", "verify", "store"],
        work=str(tmp_path / "work"),
    )
    call(command="promote", spec=str(spec))
    call(command="fetch", spec=str(spec))


def docker(*arguments: str) -> str:
    return subprocess.run(
        [docker_binary(), *arguments], check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.integration
def test_instance_copies_serve_the_image(
    source: PostgresScratch, tmp_path: Path, keys: Path
) -> None:
    spec = write_spec(source, tmp_path, keys, "instance")
    build_and_stage(spec, tmp_path)
    made = call(command="replicate", spec=str(spec), owner="mr-1")
    assert isinstance(made, CopiesResponse)
    (copy,) = made.result.created
    assert copy.secrets == {}
    env = dict(line.split("=", 1) for line in (tmp_path / "clone.env").read_text().splitlines())
    with psycopg.connect(
        host=env["DATADOUBLE_HOST"],
        port=int(env["DATADOUBLE_PORT"]),
        dbname=env["DATADOUBLE_DATABASE"],
        user=env["DATADOUBLE_USER"],
        password=env["DATADOUBLE_PASSWORD"],
    ) as connection:
        rows = connection.execute("SELECT id FROM public.items ORDER BY id").fetchall()
    assert rows == [(1,), (2,), (3,)]
    assert docker("ps", "--quiet", "--filter", f"name=datadouble-{copy.id}").strip()
    call(command="release", spec=str(spec), owner="mr-1")
    assert not docker("ps", "--all", "--quiet", "--filter", f"name=datadouble-{copy.id}").strip()
    assert not (tmp_path / "clone.env").exists()


@pytest.mark.integration
def test_volume_copies(source: PostgresScratch, tmp_path: Path, keys: Path) -> None:
    spec = write_spec(source, tmp_path, keys, "volume")
    build_and_stage(spec, tmp_path)
    made = call(command="replicate", spec=str(spec), owner="mr-2")
    assert isinstance(made, CopiesResponse)
    (copy,) = made.result.created
    volume = str(copy.location["volume"])
    listing = docker(
        "run",
        "--rm",
        "--volume",
        f"{volume}:/data",
        "--entrypoint",
        "ls",
        SERVER,
        "-n",
        "/data/pgdata",
    )
    assert "PG_VERSION" in listing
    assert " 999 " in listing
    call(command="release", spec=str(spec), copy_ids=[copy.id])
    with pytest.raises(subprocess.CalledProcessError):
        docker("volume", "inspect", volume)


def test_missing_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert DockerSettings().kind == "volume"
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ReplicateError, match="docker command was not found"):
        docker_binary()


FAKE_DOCKER = """
import json, sys
from pathlib import Path
state_file = Path(sys.argv[0]).with_name("state.json")
state = json.loads(state_file.read_text())
args = sys.argv[1:]
command = args[0] if args[0] != "volume" else "volume " + args[1]
state.setdefault("calls", []).append(args)
state_file.write_text(json.dumps(state))
if command in state.get("fail", {}):
    sys.stderr.write(state["fail"][command])
    sys.exit(1)
if command == "run" and "--interactive" in args:
    sys.stdin.buffer.read()
elif command == "inspect":
    print("true" if state.get("running", True) else "false")
elif command == "logs":
    print(state.get("logs", ""), end="")
elif command == "exec":
    state["polls"] = state.get("polls", 0) + 1
    state_file.write_text(json.dumps(state))
    sys.exit(0 if state["polls"] >= state.get("ready_after", 1) else 1)
elif command == "port":
    print("0.0.0.0:49153")
"""


class _Engine:
    """Just enough of an engine adapter to plan a server."""

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        return ServerPlan(
            image=server_image or "engine:1",
            mount="/data",
            owner="9:9",
            command=("serve", "--now"),
            ready=("check",),
            port=5432,
            database=image.name,
            user="owner",
        )


def _no_sleep(_seconds: float) -> None:
    return


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "bin" / "docker"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}", encoding="utf-8")
    script.chmod(0o755)
    state = script.with_name("state.json")
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(time, "sleep", _no_sleep)
    return state


def configure(state: Path, **values: object) -> None:
    current = json.loads(state.read_text())
    current.update(values)
    state.write_text(json.dumps(current))


def replicator(
    kind: CopyKind, *, network: str | None = None, server_image: str | None = None
) -> DockerReplicator:
    return DockerReplicator(
        DockerSettings(kind=kind, server_image=server_image, network=network),
        lambda _name: _Engine(),
    )


def summary() -> ImageSummary:
    return ImageSummary(
        identity=ImageIdentity(
            spec=SpecName("shop"),
            group=GroupName("all"),
            engine=EngineName("fake"),
            engine_version="1",
            generation=Generation("0193a7c21b4e9f30"),
        ),
        built_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        archives=(),
        manifest_sha256="0" * 64,
        verification=Verification(passed=True, checks=1, failed=0),
        strict=StrictLevel.COLUMNS,
    )


def unit_copy(copy_id: str, kind: CopyKind) -> Copy:
    return Copy(
        id=copy_id,
        prefix="shop/all/fake-1",
        generation=Generation("0193a7c21b4e9f30"),
        kind=kind,
        mode=ReplicateMode.POOL,
        adapter="docker-volume",
        state=CopyState.READY,
        created_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        built_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
    )


def image_dir(tmp_path: Path) -> Path:
    image = tmp_path / "image"
    image.mkdir(parents=True)
    (image / "file").write_text("x", encoding="utf-8")
    return image


def test_instances_with_a_stand_in_docker(tmp_path: Path, fake_docker: Path) -> None:
    configure(fake_docker, ready_after=3)
    instance = replicator(CopyKind.INSTANCE, network="ci", server_image="custom:2")
    made = instance.create("abc", image_dir(tmp_path), summary())
    assert made.location == {
        "host": "localhost",
        "port": 49153,
        "database": "image",
        "user": "owner",
        "container": "datadouble-abc",
        "volume": "datadouble-abc",
    }
    assert len(made.secrets["password"]) == 32
    calls = json.loads(fake_docker.read_text())["calls"]
    start = next(c for c in calls if "--detach" in c)
    assert start[start.index("--network") : start.index("--network") + 2] == ["--network", "ci"]
    assert "DATADOUBLE_PASSWORD" in start
    assert made.secrets["password"] not in " ".join(start)
    assert start[start.index("--entrypoint") + 1 :] == ["serve", "custom:2", "--now"]
    instance.destroy(unit_copy("abc", CopyKind.INSTANCE))
    assert ["rm", "--force", "--volumes", "datadouble-abc"] in json.loads(fake_docker.read_text())[
        "calls"
    ]
    assert instance.claim_state(unit_copy("abc", CopyKind.INSTANCE)) is ClaimState.UNKNOWN


def test_servers_that_stop_or_never_answer(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(fake_docker, running=False, logs="FATAL: data directory has wrong ownership\n")
    with pytest.raises(ReplicateError, match="stopped: FATAL: data directory has wrong ownership"):
        replicator(CopyKind.INSTANCE).create("a", image_dir(tmp_path), summary())
    configure(fake_docker, logs="")
    with pytest.raises(ReplicateError, match="stopped: no output"):
        replicator(CopyKind.INSTANCE).create("b", image_dir(tmp_path / "b"), summary())
    configure(fake_docker, running=True, ready_after=1000)
    monkeypatch.setattr(datadouble_docker, "_READY_SECONDS", -1)
    with pytest.raises(ReplicateError, match="was not ready after -1 seconds"):
        replicator(CopyKind.INSTANCE).create("c", image_dir(tmp_path / "c"), summary())


def test_docker_failures(tmp_path: Path, fake_docker: Path) -> None:
    configure(fake_docker, fail={"volume create": "Error: no space left on device\n"})
    with pytest.raises(ReplicateError, match="creating the volume failed: Error: no space left"):
        replicator(CopyKind.VOLUME).create("a", image_dir(tmp_path), summary())
    configure(fake_docker, fail={"volume create": ""})
    with pytest.raises(ReplicateError, match="creating the volume failed: status 1"):
        replicator(CopyKind.VOLUME).create("b", image_dir(tmp_path / "b"), summary())
