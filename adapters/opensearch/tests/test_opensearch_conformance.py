# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against the generated indices in the Compose opensearch service.

The service holds the same indices as the elasticsearch service, so the
sample spec is the Elasticsearch one with OpenSearch's engine, URL and image.
"""

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_elasticsearch import ElasticEngine
from datadouble_opensearch import OPENSEARCH
from test_elasticsearch_conformance import IMAGE as ELASTICSEARCH_IMAGE
from test_elasticsearch_conformance import SAMPLE_SPEC as ELASTICSEARCH_SPEC
from test_elasticsearch_conformance import URL as ELASTICSEARCH_URL
from test_elasticsearch_conformance import scratch

pytestmark = pytest.mark.integration

URL = "DATADOUBLE_TEST_OPENSEARCH_URL"
IMAGE = (
    "opensearchproject/opensearch:3.9.0"
    "@sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e"
)
SAMPLE_SPEC = (
    ELASTICSEARCH_SPEC.replace("engine: elasticsearch", "engine: opensearch")
    .replace(ELASTICSEARCH_URL, URL)
    .replace(ELASTICSEARCH_IMAGE, IMAGE)
)


@contextmanager
def _scratch() -> Generator[ScratchSource]:
    with scratch(URL, OPENSEARCH) as source:
        yield source


@pytest.fixture
def harness() -> EngineHarness:
    """The OpenSearch harness."""
    return EngineHarness(
        engine=ElasticEngine(OPENSEARCH),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(table=TableName.of("", "customers"), column=ColumnName("email")),
        scratch=_scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """OpenSearch passes the engine conformance suite."""
