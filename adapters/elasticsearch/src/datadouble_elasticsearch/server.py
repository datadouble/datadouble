# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway single-node clusters, in the product's official image.

Each working database is a single node on a data directory in scratch space,
run as Datadouble's own user with the paths it needs mounted at the same
absolute path. Security is on, over plain HTTP, and Datadouble signs in as
the product's administrator. The password is generated once per working
database and kept beside its data directory, never in it, so images do not
carry it: OpenSearch stores its users in the data directory, so a node must
start again with the password it first had.

Both products exit with status 143 when they stop on SIGTERM; a small shell
around the node reports that normal stop as success.
"""

import re
import secrets
import shlex
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from datadouble.catalog import Requirement, RequirementKind
from datadouble.engine import EngineError
from datadouble.runtime import Container, ContainerRuntime
from datadouble_elasticsearch.client import Client
from datadouble_elasticsearch.product import Product

PORT = 9200
LOGIN_ENV = "DATADOUBLE_PASSWORD"
"""The environment variable that holds the administrator's password."""
LOGIN_FILE = ".login"
"""The file beside a working database's data directory that holds its password."""
_VERSION = re.compile(r"Version: (\d+\.\d+\.\d+)")
_PLUGIN_NAME = re.compile(r"^Name: (\S+)$", re.MULTILINE)
_PLUGIN_VERSION = re.compile(r"^Version: (\S+)$", re.MULTILINE)
_STOP = (
    "stop() { kill -TERM $pid; wait $pid; status=$?; [ $status -eq 143 ] && exit 0; exit $status; }"
)
"""Shell that stops the node and reports its stop on SIGTERM as success."""
_HEALTH = "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"


class ServerSettings(BaseModel):
    """The ``sources.<name>.server`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    heap: str = Field(default="512m", pattern=r"^\d+[mg]$")
    """The Java heap of working nodes."""


def curl(product: Product, arguments: str) -> str:
    """Shell for one request to the local node as the administrator; fails on an HTTP error.

    curl reads its configuration, and so the password, from standard input,
    never its command line.
    """
    auth = f'printf \'user = "{product.user}:%s"\\n\' "${LOGIN_ENV}"'
    return f"{auth} | curl -sf -K- -o /dev/null -H 'content-type: application/json' {arguments}"


def start_command(product: Product, data: str, repo: str | None, heap: str, then: str = "") -> str:
    """Shell that runs a node on ``data``, runs ``then`` once it answers, and waits.

    A failure in ``then`` stops the node and the container.
    """
    settings = [*product.settings, f"path.data={data}", *([f"path.repo={repo}"] if repo else [])]
    lines = [
        product.setup,
        f'export {product.heap}="-Xms{heap} -Xmx{heap}"',
        f"{product.launch} {' '.join(f'-E{setting}' for setting in settings)} &",
        "pid=$!",
        _STOP,
        "trap stop TERM INT",
    ]
    if then:
        health = curl(product, shlex.quote(_HEALTH))
        lines += [
            f"until {health}; do kill -0 $pid || exit 1; sleep 1; done",
            f"{{ {then}; }} || {{ kill -TERM $pid; wait $pid; exit 1; }}",
        ]
    lines.append("wait $pid")
    return "\n".join(lines)


def ready_command(product: Product, marker: str = "") -> tuple[str, ...]:
    """A command that succeeds once the node answers, and ``marker`` exists if given."""
    check = f"test -f {marker} && " if marker else ""
    return ("sh", "-c", check + curl(product, shlex.quote(_HEALTH)))


@dataclass(frozen=True, slots=True)
class Tools:
    """A product's nodes in containers from one image."""

    product: Product
    runtime: ContainerRuntime
    image: str
    heap: str = "512m"

    def version(self, owner: Path) -> str:
        """The version of the image's server, such as ``9.5.5``."""
        command = (self.product.engine, "--version")
        done = self.runtime.run(Container(image=self.image, command=command, owner=owner))
        match = _VERSION.search(done.stdout)
        if done.status != 0 or match is None:
            detail = (done.stderr or done.stdout).strip()[-80:]
            message = f"cannot read the {self.product.name} version: {detail!r}"
            raise EngineError(message)
        return match.group(1)

    def plugins(self, owner: Path) -> tuple[Requirement, ...]:
        """The plugins installed in the image, from its plugin tool."""
        command = (f"{self.product.engine}-plugin", "list", "--verbose")
        done = self.runtime.run(Container(image=self.image, command=command, owner=owner))
        if done.status != 0:
            detail = (done.stderr or done.stdout).strip()[-80:]
            message = f"cannot list the {self.product.name} image's plugins: {detail!r}"
            raise EngineError(message)
        names = _PLUGIN_NAME.findall(done.stdout)
        versions = _PLUGIN_VERSION.findall(done.stdout)
        return tuple(
            Requirement(kind=RequirementKind.PLUGIN, name=name, version=version)
            for name, version in zip(names, versions, strict=True)
        )


def _password(directory: Path) -> str:
    """The working database's password, made on its first start."""
    file = directory / LOGIN_FILE
    if not file.exists():
        file.touch(mode=0o600)
        file.write_text(secrets.token_hex(16), "utf-8")
    return file.read_text("utf-8")


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
        """Start the node for the block, then stop it cleanly; yields a client."""
        self.data.mkdir(parents=True, exist_ok=True)
        if self.repo:
            self.repo.mkdir(parents=True, exist_ok=True)
        password = _password(self.data.parent)
        product = self.tools.product
        command = start_command(
            product, str(self.data), str(self.repo) if self.repo else None, self.tools.heap
        )
        container = Container(
            image=self.tools.image,
            command=("sh", "-c", command),
            owner=self.owner,
            mounts=tuple(self.mounts),
            secrets={LOGIN_ENV: password},
            port=PORT,
            ready=ready_command(product),
        )
        with self.tools.runtime.serve(container) as server:
            client = Client.at(server.endpoint.host, server.endpoint.port, password, product)
            try:
                yield client
            finally:
                client.close()
