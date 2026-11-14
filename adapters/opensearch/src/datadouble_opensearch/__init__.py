# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The OpenSearch engine adapter for Datadouble.

Spec settings::

    source:
      engine: opensearch
      connection:
        url: {env: SEARCH_URL}      # http[s]://[user:password@]host[:port], by reference
        indices: ["*", "-logs-*"]   # index patterns; the default is every open index
        sample: 1000                # documents sampled for fields mappings do not declare
      server:
        heap: 512m                  # the Java heap of working nodes

The source is OpenSearch 2.4 or later, the first with points in time. The
adapter is the Elasticsearch adapter's package serving OpenSearch
(decision 96): the same catalog, extract, images and rules, with
OpenSearch's image, security plugin and point-in-time API. Working nodes and
copies run the security plugin's demonstration configuration over plain
HTTP, with the ``admin`` user's password set at their first start.
"""

from datadouble.names import EngineName
from datadouble_elasticsearch import ElasticEngine
from datadouble_elasticsearch.product import COMMON_SETTINGS, PointInTimeApi, Product

_HOME = "/usr/share/opensearch"

OPENSEARCH = Product(
    engine=EngineName("opensearch"),
    name="OpenSearch",
    distribution="opensearch",
    owner="1000:1000",
    repository="opensearchproject/opensearch",
    user="admin",
    launch=f"{_HOME}/opensearch-docker-entrypoint.sh opensearch",
    # The image's entrypoint would refuse a password without capitals and
    # symbols; the demonstration installer takes any with -t. Once a node has
    # started, its users live in its data directory and this changes nothing.
    setup=(
        "export DISABLE_INSTALL_DEMO_CONFIG=true"
        f' && OPENSEARCH_INITIAL_ADMIN_PASSWORD="$DATADOUBLE_PASSWORD"'
        f" bash {_HOME}/plugins/opensearch-security/tools/install_demo_configuration.sh"
        " -y -i -s -t > /dev/null"
    ),
    heap="OPENSEARCH_JAVA_OPTS",
    settings=(
        *COMMON_SETTINGS,
        "plugins.security.ssl.http.enabled=false",
        "plugins.security.audit.type=noop",
    ),
    pit=PointInTimeApi(
        open="_search/point_in_time", key="pit_id", close="/_search/point_in_time", listed=True
    ),
)


def engine() -> ElasticEngine:
    """The entry point named in ``datadouble.engines``."""
    return ElasticEngine(OPENSEARCH)
