# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Docker container runtime, against the Docker-in-Docker daemon.

The test runner and the daemon share the volume temporary files live on, at
the same path, as a production work directory must be shared with its daemon.
"""

import os
from pathlib import Path

import pytest

from datadouble.runtime import Container, ContainerError, ContainerRuntime
from datadouble_docker.runtime import DockerRuntime, DockerRuntimeSettings, docker_runtime

pytestmark = pytest.mark.integration

IMAGE = "postgres:18-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
LOOP = "trap 'exit 0' INT TERM; touch /tmp/ready; while :; do sleep 0.2; done"


def runtime(**settings: str | int) -> ContainerRuntime:
    address = os.environ.get("DATADOUBLE_TEST_DOCKER_ADDRESS", "127.0.0.1")
    return docker_runtime({"publish": "0.0.0.0", "address": address, **settings})  # noqa: S104 - published inside the test daemon only


def server(tmp_path: Path, script: str = LOOP) -> Container:
    return Container(
        image=IMAGE,
        command=("sh", "-c", script),
        owner=tmp_path / "stage",
        port=5432,
        ready=("test", "-f", "/tmp/ready"),  # noqa: S108 - a path inside the container
    )


def test_jobs_share_files_and_receive_secrets(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    done = runtime(network="bridge").run(
        Container(
            image=IMAGE,
            command=("sh", "-c", f'id -u; echo "$TOKEN $MODE" > {shared}/out'),
            owner=tmp_path / "stage",
            mounts=(shared,),
            env={"MODE": "plain"},
            secrets={"TOKEN": "s3cret"},
        )
    )
    assert done.status == 0, done.stderr
    assert done.stdout.strip() == str(os.getuid())
    written = shared / "out"
    assert written.read_text("utf-8") == "s3cret plain\n"
    assert written.stat().st_uid == os.getuid()
    failed = runtime().run(
        Container(image=IMAGE, command=("sh", "-c", "echo no >&2; exit 3"), owner=tmp_path)
    )
    assert (failed.status, failed.stderr.strip()) == (3, "no")


def test_servers_publish_execute_and_stop(tmp_path: Path) -> None:
    with runtime().serve(server(tmp_path)) as running:
        assert running.endpoint.port > 0
        assert running.endpoint.host == os.environ.get(
            "DATADOUBLE_TEST_DOCKER_ADDRESS", "127.0.0.1"
        )
        done = running.execute(["sh", "-c", 'echo "$WORD"'], {"WORD": "inside"})
        assert done.stdout.strip() == "inside"


def test_server_failures(tmp_path: Path) -> None:
    with (
        pytest.raises(ContainerError, match="stopped while starting: going away"),
        runtime().serve(server(tmp_path, "echo going away; exit 3")),
    ):
        pass
    never = server(tmp_path, "trap 'exit 0' INT TERM; while :; do sleep 0.2; done")
    with (
        pytest.raises(ContainerError, match="was not ready after 1 seconds"),
        runtime(ready_seconds=1).serve(never),
    ):
        pass
    unclean = server(
        tmp_path, "trap 'echo bye; exit 4' INT TERM; touch /tmp/ready; while :; do sleep 0.2; done"
    )
    with pytest.raises(ContainerError, match="exited with status 4: bye"), runtime().serve(unclean):
        pass
    portless = Container(image=IMAGE, command=("true",), owner=tmp_path)
    with pytest.raises(ContainerError, match="needs a port"), runtime().serve(portless):
        pass
    with (
        pytest.raises(ContainerError, match=r"starting postgres:18.*failed"),
        runtime().serve(Container(image=IMAGE, command=("/nonexistent",), owner=tmp_path, port=1)),
    ):
        pass


def test_release_removes_leftovers(tmp_path: Path) -> None:
    docker = runtime()
    assert isinstance(docker, DockerRuntime)
    other = docker.serve(server(tmp_path / "elsewhere"))
    other.__enter__()
    left = docker.serve(server(tmp_path / "load"))
    left.__enter__()
    docker.release(tmp_path / "load")
    with pytest.raises(ContainerError, match="stopping the server failed"):
        left.__exit__(None, None, None)
    other.__exit__(None, None, None)


def test_missing_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ContainerError, match="docker command was not found"):
        DockerRuntime(DockerRuntimeSettings()).run(server(tmp_path))
