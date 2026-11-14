# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""OpenSearch behavior beyond the conformance suite: copies of its images.

Everything else is the Elasticsearch adapter's code, tested there.
"""

import json
import shutil
from pathlib import Path

import pytest

from datadouble.catalog import CatalogLock
from datadouble.planner import PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, Stage
from datadouble.runtime import Container
from datadouble.spec import ImageFormat, parse_spec
from datadouble.storage import filesystem_store
from datadouble_conformance.engine import runtime_for_tests
from datadouble_elasticsearch import ElasticEngine
from datadouble_elasticsearch.client import Client, document, items, path
from datadouble_elasticsearch.server import LOGIN_ENV, PORT, Tools, ready_command
from datadouble_elasticsearch.working import ElasticWorking, serve_command
from datadouble_opensearch import OPENSEARCH, engine
from test_opensearch_conformance import URL, scratch

pytestmark = pytest.mark.integration

VERSION = "3.9.0"


def test_served_copies(tmp_path: Path) -> None:
    """A copy restores its image at first start and keeps its password after."""
    ((kind, settings),) = runtime_for_tests().items()
    runtime = installed().runtime(kind, settings)
    with scratch(URL, OPENSEARCH) as source:
        for item in (1, 2, 3):
            source.insert(item)
        spec = parse_spec(
            "name: items\n"
            f"source: {{engine: opensearch, connection: {json.dumps(dict(source.connection))}}}\n"
            "subset: {seeds: {items: '{\"range\": {\"id\": {\"lte\": 2}}}'}}\n"
            f"runtime: {json.dumps(runtime_for_tests())}\n"
        )
        with engine().connect(source.connection) as session:
            lock = CatalogLock.of(session.catalog())
        registry = Registry(
            engines={"opensearch": engine},
            stores={"filesystem": filesystem_store},
            runtimes=installed().runtimes,
        )
        stages = (Stage.PLAN, Stage.EXTRACT, Stage.LOAD, Stage.TRANSFORM)
        options = RunOptions(stages=stages, work=tmp_path / "work", plan=PlanOptions())
        result = Run(spec, lock, registry, options).execute()
    image = tmp_path / "image"
    image.mkdir()
    tools = Tools(OPENSEARCH, runtime, OPENSEARCH.image(VERSION))
    assert ElasticWorking(tools, tmp_path / "scratch").server_version() == VERSION
    ElasticWorking(tools, tmp_path / "scratch").package(
        Path(result.work) / "transform" / "all" / "db", ImageFormat.PHYSICAL, image
    )
    plan = ElasticEngine(OPENSEARCH).server(image, None)
    assert (plan.image, plan.user, plan.owner) == (OPENSEARCH.image(VERSION), "admin", "1000:1000")
    # The plan's mount is a fixed path; serve the copy where it is instead,
    # with a password like the ones replicate adapters make.
    copy = tmp_path / "copy"
    shutil.copytree(image, copy)
    command, marker = serve_command(OPENSEARCH, str(copy), "512m")
    password = "0123456789abcdef0123456789abcdef"
    container = Container(
        image=OPENSEARCH.image(VERSION),
        command=("sh", "-c", command),
        owner=copy,
        mounts=(copy,),
        secrets={LOGIN_ENV: password},
        port=PORT,
        ready=ready_command(OPENSEARCH, marker),
    )
    for _start in range(2):
        with runtime.serve(container) as server:
            client = Client.at(server.endpoint.host, server.endpoint.port, password, OPENSEARCH)
            reply = client.request("POST", path("items", "_search"), "reading", body={})
            hits = items(document(reply.get("hits"), "hits").get("hits"), "hits")
            assert sorted(str(document(hit, "a hit").get("_id")) for hit in hits) == ["1", "2"]
            client.close()
