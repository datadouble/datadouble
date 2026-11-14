# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway Elasticsearch nodes, in the official Elasticsearch image.

Each working database is a single node on a data directory in scratch space,
run as Datadouble's own user with the paths it needs mounted at the same
absolute path. Security is on, over plain HTTP: the ``elastic`` user's
password is generated for each start and reaches the node as its bootstrap
password, which lives in the container's own keystore, never in the data
directory, so images do not carry it.

Elasticsearch exits with status 143 when it stops on SIGTERM; a small shell
around it reports that normal stop as success.
"""

import re
import secrets
import shlex
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from datadouble.engine import EngineError
from datadouble.runtime import Container, ContainerRuntime
from datadouble_elasticsearch.client import Client

PORT = 9200
LOGIN_ENV = "DATADOUBLE_PASSWORD"
"""The environment variable that holds the ``elastic`` user's password."""
_VERSION = re.compile(r"Version: (\d+\.\d+\.\d+)")
_SETTINGS = (
    "discovery.type=single-node",
    "xpack.security.enabled=true",
    "xpack.security.http.ssl.enabled=false",
    "xpack.security.transport.ssl.enabled=false",
    # No memory-mapped index files, which would need the host's vm.max_map_count raised.
    "node.store.allow_mmap=false",
    "cluster.routing.allocation.disk.threshold_enabled=false",
    "cluster.deprecation_indexing.enabled=false",
    "ingest.geoip.downloader.enabled=false",
    "xpack.ml.enabled=false",
    "xpack.watcher.enabled=false",
)
_AUTH = f'printf \'user = "elastic:%s"\\n\' "${LOGIN_ENV}"'
"""Shell that writes curl's configuration for the ``elastic`` user, keeping the password
off curl's command line."""
_STOP = (
    "stop() { kill -TERM $pid; wait $pid; status=$?; [ $status -eq 143 ] && exit 0; exit $status; }"
)
"""Shell that stops the node and reports its stop on SIGTERM as success."""
_HEALTH = "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"


class ServerSettings(BaseModel):
    """The ``source.server`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    heap: str = Field(default="512m", pattern=r"^\d+[mg]$")
    """The Java heap of working nodes."""


def default_image(server_version: str) -> str:
    """The official image of exactly ``server_version``, such as ``elasticsearch:9.5.5``."""
    return f"elasticsearch:{server_version}"


def curl(arguments: str) -> str:
    """Shell for one request to the local node as ``elastic``; fails on an HTTP error."""
    return f"{_AUTH} | curl -sf -K- -o /dev/null -H 'content-type: application/json' {arguments}"


def start_command(data: str, repo: str | None, heap: str, then: str = "") -> str:
    """Shell that runs a node on ``data``, runs ``then`` once it answers, and waits.

    A failure in ``then`` stops the node and the container.
    """
    settings = [*_SETTINGS, f"path.data={data}", *([f"path.repo={repo}"] if repo else [])]
    lines = [
        f'export ELASTIC_PASSWORD="${LOGIN_ENV}" ES_JAVA_OPTS="-Xms{heap} -Xmx{heap}"',
        "/usr/local/bin/docker-entrypoint.sh eswrapper "
        + " ".join(f"-E{setting}" for setting in settings)
        + " &",
        "pid=$!",
        _STOP,
        "trap stop TERM INT",
    ]
    if then:
        lines += [
            f"until {curl(shlex.quote(_HEALTH))}; do kill -0 $pid || exit 1; sleep 1; done",
            f"{{ {then}; }} || {{ kill -TERM $pid; wait $pid; exit 1; }}",
        ]
    lines.append("wait $pid")
    return "\n".join(lines)


def ready_command(marker: str = "") -> tuple[str, ...]:
    """A command that succeeds once the node answers, and ``marker`` exists if given."""
    check = f"test -f {marker} && " if marker else ""
    return ("sh", "-c", check + curl(shlex.quote(_HEALTH)))


@dataclass(frozen=True, slots=True)
class Tools:
    """Elasticsearch in containers from one image."""

    runtime: ContainerRuntime
    image: str
    heap: str = "512m"

    def version(self, owner: Path) -> str:
        """The version of the image's Elasticsearch, such as ``9.5.5``."""
        done = self.runtime.run(
            Container(image=self.image, command=("elasticsearch", "--version"), owner=owner)
        )
        match = _VERSION.search(done.stdout)
        if done.status != 0 or match is None:
            detail = (done.stderr or done.stdout).strip()[-80:]
            message = f"cannot read the Elasticsearch version: {detail!r}"
            raise EngineError(message)
        return match.group(1)


class ElasticServer:
    """One node, identified by its data directory."""

    def __init__(self, tools: Tools, data: Path, owner: Path, repo: Path | None = None) -> None:
        """Manage the node on ``data`` for the stage directory ``owner``.

        ``repo`` is a directory the node may keep snapshot repositories in.
        """
        self.tools = tools
        self.data = data
        self.owner = owner
        self.repo = repo

    @property
    def mounts(self) -> Sequence[Path]:
        """The paths the node's container sees."""
        return (self.data.parent, *([self.repo] if self.repo else []))

    @contextmanager
    def running(self) -> Generator[Client]:
        """Start the node for the block, then stop it cleanly; yields a client as ``elastic``."""
        self.data.mkdir(parents=True, exist_ok=True)
        if self.repo:
            self.repo.mkdir(parents=True, exist_ok=True)
        password = secrets.token_hex(16)
        command = start_command(
            str(self.data), str(self.repo) if self.repo else None, self.tools.heap
        )
        container = Container(
            image=self.tools.image,
            command=("sh", "-c", command),
            owner=self.owner,
            mounts=tuple(self.mounts),
            secrets={LOGIN_ENV: password},
            port=PORT,
            ready=ready_command(),
        )
        with self.tools.runtime.serve(container) as server:
            client = Client.at(server.endpoint.host, server.endpoint.port, password)
            try:
                yield client
            finally:
                client.close()
