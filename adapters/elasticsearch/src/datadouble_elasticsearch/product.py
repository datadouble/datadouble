# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""What differs between Elasticsearch and OpenSearch.

OpenSearch began as a fork of Elasticsearch 7.10, and both still serve the
REST API this package uses: mappings, ``search_after`` searches, bulk
requests, snapshot repositories and Painless. They differ in their images,
their security and their point-in-time API, which a :class:`Product`
describes. The ``opensearch`` adapter is this package with its own product
(decision 96).
"""

from dataclasses import dataclass

from pydantic import JsonValue

from datadouble.names import EngineName


@dataclass(frozen=True, slots=True)
class PointInTimeApi:
    """Where a product opens and closes points in time."""

    open: str
    """The path after the target indices, such as ``_pit``."""
    key: str
    """The field of the reply that holds the new point in time's ID."""
    close: str
    """The path that closes one."""
    listed: bool
    """Whether closing takes a list of IDs."""

    def close_body(self, pit: str) -> dict[str, JsonValue]:
        """The body that closes ``pit``."""
        return {self.key: [pit] if self.listed else pit}


@dataclass(frozen=True, slots=True)
class Product:
    """One search engine this package serves."""

    engine: EngineName
    name: str
    """The product's name in messages, such as ``Elasticsearch``."""
    distribution: str
    """``version.distribution`` in its ``GET /`` reply (Elasticsearch's has none)."""
    owner: str
    """The ``uid:gid`` its official image serves as."""
    repository: str
    """Its official image's repository on Docker Hub."""
    user: str
    """The built-in administrator working nodes and copies authenticate as."""
    launch: str
    """Shell that starts a node in the image; settings follow as ``-E`` options."""
    setup: str
    """Shell run before ``launch``, with the password in ``$DATADOUBLE_PASSWORD``."""
    heap: str
    """The environment variable for the node's Java options."""
    settings: tuple[str, ...]
    """Settings for every working node and copy."""
    pit: PointInTimeApi

    def image(self, server_version: str) -> str:
        """The official image of exactly ``server_version``."""
        return f"{self.repository}:{server_version}"


_COMMON = (
    "discovery.type=single-node",
    # No memory-mapped index files, which would need the host's vm.max_map_count raised.
    "node.store.allow_mmap=false",
    "cluster.routing.allocation.disk.threshold_enabled=false",
)

ELASTICSEARCH = Product(
    engine=EngineName("elasticsearch"),
    name="Elasticsearch",
    distribution="elasticsearch",
    owner="1000:0",
    repository="elasticsearch",
    user="elastic",
    launch="/usr/local/bin/docker-entrypoint.sh eswrapper",
    # The password is the node's bootstrap password, kept in the container's keystore.
    setup='export ELASTIC_PASSWORD="$DATADOUBLE_PASSWORD"',
    heap="ES_JAVA_OPTS",
    settings=(
        *_COMMON,
        "xpack.security.enabled=true",
        "xpack.security.http.ssl.enabled=false",
        "xpack.security.transport.ssl.enabled=false",
        "cluster.deprecation_indexing.enabled=false",
        "ingest.geoip.downloader.enabled=false",
        "xpack.ml.enabled=false",
        "xpack.watcher.enabled=false",
    ),
    pit=PointInTimeApi(open="_pit", key="id", close="/_pit", listed=False),
)
