# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` creates the group's indices on a fresh node from the extract's dump
and applies the fallbacks for missing parents. ``transform`` copies the
stopped node's data directory and anonymizes the copy, so an interrupted
transform leaves the loaded database untouched: value rules (faker, hash,
null and the like) rewrite each index in one pass, then each expression rule
runs as an ``_update_by_query`` with its Painless script. ``package`` writes
a snapshot repository (physical) or a dump (logical); ``restore`` brings
either back up.
"""

import datetime as dt
import json
import shlex
import shutil
from collections import defaultdict
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_elasticsearch.client import Client, Document, as_document, as_list, document
from datadouble_elasticsearch.client import path as request_path
from datadouble_elasticsearch.documents import Dump, entry, rewrite, values
from datadouble_elasticsearch.indices import export, load, put, refresh
from datadouble_elasticsearch.search import ID, PointInTime, Query, parse
from datadouble_elasticsearch.server import ElasticServer, Tools, curl, start_command
from datadouble_elasticsearch.source import held

DATA = "data"
REPO = "repo"
INFO = "database.json"
_DUMP = "dump"
_REPOSITORY = "/_snapshot/datadouble"
_SNAPSHOT = f"{_REPOSITORY}/golden"
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_ALL: Query = {"match_all": {}}


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    server_version: str
    indices: tuple[str, ...]

    @classmethod
    def read(cls, directory: Path) -> "DatabaseInfo":
        """Read ``database.json`` from ``directory``."""
        return cls.model_validate_json((directory / INFO).read_bytes())


def _single(edge: Edge) -> tuple[ColumnName, ColumnName]:
    if len(edge.columns) != 1:
        message = f"{edge.name}: Elasticsearch references must have one field on each side"
        raise EngineError(message)
    return edge.columns[0], edge.parent_columns[0]


def _when(edge: Edge) -> Query:
    return parse(edge.when, _EPOCH, f"{edge.name} `when`") if edge.when else _ALL


def _fields(column: ColumnName) -> list[str]:
    return [] if column == ID else [column]


def column_values(pit: PointInTime, index: str, column: ColumnName) -> set[str]:
    """Every value, as text, an index holds in ``column``."""
    return {found for hit in pit.hits(index, _ALL, _fields(column)) for found in held(hit, column)}


def _with_source(hit: Mapping[str, object], source: object) -> Document:
    return {**entry(hit), "_source": document(source, "a document")}


class ElasticRestored:
    """A cluster open for checks: a restored image, or a working database."""

    def __init__(self, client: Client) -> None:
        """Wrap a client."""
        self.client = client

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Documents per index."""
        found: dict[TableName, int] = {}
        for table in tables:
            reply = self.client.request(
                "GET", request_path(table.name, "_count"), f"counting {table}"
            )
            found[table] = int(str(reply.get("count")))
        return found

    def orphans(self, edge: Edge) -> int:
        """Child documents with a reference (or array element) that has no parent."""
        child, parent = _single(edge)
        targets = sorted({edge.child.name, edge.parent.name})
        with PointInTime.opened(self.client, targets) as pit:
            parents = column_values(pit, edge.parent.name, parent)
            return sum(
                1
                for hit in pit.hits(edge.child.name, _when(edge), _fields(child))
                if any(found not in parents for found in held(hit, child))
            )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct string values of a field, streamed."""
        seen: set[str] = set()
        parts = column.column.split(".")
        with PointInTime.opened(self.client, [column.table.name]) as pit:
            for hit in pit.hits(column.table.name, _ALL, _fields(column.column)):
                held = (
                    [hit.get("_id")]
                    if column.column == ID
                    else values(as_document(hit.get("_source")) or {}, parts)
                )
                for found in held:
                    if isinstance(found, str) and found not in seen:
                        seen.add(found)
                        yield found

    def failing_rows(self, query: str) -> int:
        """Documents an assertion matches: ``{"index": ..., "query": {...}}``."""
        assertion = parse(query, _EPOCH, "an assertion")
        index, matched = assertion.get("index"), as_document(assertion.get("query"))
        if not isinstance(index, str) or matched is None:
            message = 'an Elasticsearch assertion is {"index": "...", "query": {...}}'
            raise EngineError(message)
        reply = self.client.request(
            "POST",
            request_path(index, "_count"),
            "running an assertion",
            body={"query": assertion["query"]},
        )
        return int(str(reply.get("count")))


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


def apply_fixup(client: Client, fixup: Fixup) -> None:
    """Null references to missing parents, or index stub parents for them."""
    edge = fixup.edge
    child, parent = _single(edge)
    action = f"applying the fallback for {edge.name}"
    # The parent's index is missing when the parent is outside the group.
    with PointInTime.opened(client, sorted({edge.child.name, edge.parent.name})) as pit:
        present = (
            column_values(pit, edge.parent.name, parent) if fixup.parent_present else set[str]()
        )
        orphaned = [
            hit
            for hit in pit.hits(edge.child.name, _when(edge))
            if any(found not in present for found in held(hit, child))
        ]
    if fixup.mode is EdgeMode.NULL:
        lines = (
            _with_source(hit, rewrite(hit.get("_source"), child.split("."), lambda _: None))
            for hit in orphaned
        )
        if put(client, edge.child.name, lines, action):
            refresh(client, [edge.child.name])
    else:
        missing = sorted({found for hit in orphaned for found in held(hit, child)} - present)
        stubs: list[Document] = [
            {"_id": value, "_source": {}}
            if parent == ID
            else {"_id": f"stub-{value}", "_source": {parent: value}}
            for value in missing
        ]
        if put(client, edge.parent.name, stubs, action):
            refresh(client, [edge.parent.name])


def _typed(original: object, replacement: str) -> object:
    """Keep integer fields integers when a hash replaced them."""
    if isinstance(original, int) and not isinstance(original, bool) and replacement.isdigit():
        return int(replacement)
    return replacement


def _replaceable(value: object) -> bool:
    return isinstance(value, str | int) and not isinstance(value, bool)


def changer(rule: PlannedRule, mapping: Mapping[str, str]) -> Callable[[object], object]:
    """What a value rule does to a field: null it, or swap each value for its replacement."""
    if isinstance(rule.rule, NullRule):
        return lambda _: None

    def swap(value: object) -> object:
        if _replaceable(value) and str(value) in mapping:
            return _typed(value, mapping[str(value)])
        return value

    def change(value: object) -> object:
        listed = as_list(value)
        return [swap(item) for item in listed] if listed is not None else swap(value)

    return change


def _originals(pit: PointInTime, index: str, rules: list[PlannedRule]) -> dict[str, set[str]]:
    """The distinct replaceable values of each rule's column."""
    found: dict[str, set[str]] = {rule.column.column: set() for rule in rules}
    if not rules:
        return found
    for hit in pit.hits(index, _ALL, list(found)):
        source = as_document(hit.get("_source")) or {}
        for column, seen in found.items():
            seen.update(str(v) for v in values(source, column.split(".")) if _replaceable(v))
    return found


def anonymize(
    client: Client, index: str, rules: list[PlannedRule], transformation: Transformation
) -> None:
    """Apply an index's rules: value rules in one rewrite, then each expression."""
    plain = [rule for rule in rules if not isinstance(rule.rule, ExpressionRule)]
    with PointInTime.opened(client, [index]) as pit:
        mapped = [rule for rule in plain if not isinstance(rule.rule, NullRule)]
        originals = _originals(pit, index, mapped)
        changers = [
            (
                rule.column.column.split("."),
                changer(
                    rule,
                    {}
                    if isinstance(rule.rule, NullRule)
                    else transformation.mapper(rule, sorted(originals[rule.column.column])),
                ),
            )
            for rule in plain
        ]

        def rewritten() -> Iterator[Document]:
            for hit in pit.hits(index, _ALL):
                source: object = hit.get("_source")
                for parts, change in changers:
                    source = rewrite(source, parts, change)
                if source != hit.get("_source"):
                    yield _with_source(hit, source)

        if changers and put(client, index, rewritten(), f"anonymizing {index}"):
            # _update_by_query fails on documents changed since the last refresh.
            refresh(client, [index])
    for rule in rules:
        if isinstance(rule.rule, ExpressionRule):
            _expression(client, index, rule, rule.rule)
    refresh(client, [index])


def _expression(client: Client, index: str, rule: PlannedRule, expression: ExpressionRule) -> None:
    where = (
        parse(expression.where, _EPOCH, f"the `where` for {rule.column}")
        if expression.where
        else _ALL
    )
    body: dict[str, JsonValue] = {
        "script": {
            "source": expression.expression,
            "lang": "painless",
            "params": {"column": rule.column.column},
        },
        "query": where,
    }
    # A failed document fails the request: its status is the worst failure's.
    client.request(
        "POST",
        request_path(index, "_update_by_query"),
        f"anonymizing {rule.column}",
        body=body,
        params={"refresh": True, "conflicts": "abort", "wait_for_completion": True},
    )


def snapshot_repository(location: str, *, readonly: bool) -> dict[str, JsonValue]:
    """The body that registers the shared-filesystem repository at ``location``."""
    return {"type": "fs", "settings": {"location": location, "readonly": readonly}}


def check_snapshot(reply: Mapping[str, object]) -> None:
    """Fail unless a finished snapshot copied every shard."""
    state = document(reply.get("snapshot"), "the snapshot").get("state")
    if state != "SUCCESS":
        message = f"the snapshot finished as {state}"
        raise EngineError(message)


class ElasticWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every node under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's Elasticsearch."""
        if self._version is None:
            self._version = self.tools.version(self.scratch)
        return self._version

    def load(
        self,
        plan: Plan,  # noqa: ARG002 - the protocol passes the plan; loading needs only the group
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Create and fill the group's indices and apply its fallbacks."""
        if hooks:
            message = "the elasticsearch adapter does not run hooks"
            raise EngineError(message)
        members = {table.name for table in group.tables}
        dump = Dump.read(extract)
        server = ElasticServer(self.tools, directory / DATA, directory)
        with server.running() as client:
            load(client, extract, members)
            for fixup in group.fixups:
                apply_fixup(client, fixup)
            counts = ElasticRestored(client).counts(group.tables)
        info = DatabaseInfo(
            server_version=self.server_version(),
            indices=tuple(sorted(f.index for f in dump.indices if f.index in members)),
        )
        (directory / INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy."""
        if transformation.hooks:
            message = "the elasticsearch adapter does not run hooks"
            raise EngineError(message)
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        by_index: defaultdict[str, list[PlannedRule]] = defaultdict(list)
        for rule in transformation.rules:
            by_index[rule.column.table.name].append(rule)
        server = ElasticServer(self.tools, directory / DATA, directory)
        with server.running() as client:
            for index, rules in by_index.items():
                anonymize(client, index, rules, transformation)
            counts = ElasticRestored(client).counts(group.tables)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        shutil.copyfile(source / INFO, directory / INFO)
        info = DatabaseInfo.read(source)
        if image_format is ImageFormat.PHYSICAL:
            repo = directory / REPO
            server = ElasticServer(self.tools, source / DATA, directory, repo)
            with server.running() as client:
                client.request(
                    "PUT",
                    _REPOSITORY,
                    "registering the snapshot repository",
                    body=snapshot_repository(str(repo), readonly=False),
                )
                reply = client.request(
                    "PUT",
                    _SNAPSHOT,
                    "taking the snapshot",
                    body={"indices": list(info.indices), "include_global_state": False},
                    params={"wait_for_completion": True},
                )
                check_snapshot(reply)
                client.request("DELETE", _REPOSITORY, "releasing the snapshot repository")
            return
        target = directory / _DUMP
        target.mkdir()
        server = ElasticServer(self.tools, source / DATA, directory)
        with server.running() as client, PointInTime.opened(client, info.indices) as pit:
            files = [
                export(pit, index, _ALL, target / f"{number:05}.ndjson.gz")
                for number, index in enumerate(info.indices)
            ]
        Dump(indices=tuple(files)).write(target)

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up on a scratch node for checks."""
        directory.mkdir(parents=True, exist_ok=True)
        physical = image_format is ImageFormat.PHYSICAL
        repo = image / REPO if physical else None
        server = ElasticServer(self.tools, directory / DATA, directory, repo)
        with server.running() as client:
            if physical:
                client.request(
                    "PUT",
                    _REPOSITORY,
                    "registering the snapshot repository",
                    body=snapshot_repository(str(image / REPO), readonly=True),
                )
                client.request(
                    "POST",
                    f"{_SNAPSHOT}/_restore",
                    "restoring the snapshot",
                    body={"include_global_state": False},
                    params={"wait_for_completion": True},
                )
            else:
                load(client, image / _DUMP)
            yield ElasticRestored(client)


def serve_command(mount: str, heap: str) -> tuple[str, str]:
    """The shell that serves a copy of a physical image, and the file it marks restored.

    The first start restores the snapshot into the copy's data directory;
    later starts serve what is there.
    """
    marker = f"{mount}/restored"
    base = "http://127.0.0.1:9200"
    register = json.dumps(snapshot_repository(f"{mount}/{REPO}", readonly=True))
    restore = json.dumps({"include_global_state": False})
    restore_url = f"{base}{_SNAPSHOT}/_restore?wait_for_completion=true"
    steps = [
        curl(f"-X PUT {base}{_REPOSITORY} -d {shlex.quote(register)}"),
        curl(f"-X POST {shlex.quote(restore_url)} -d {shlex.quote(restore)}"),
        f"touch {marker}",
    ]
    then = f"[ -f {marker} ] || {{ {' && '.join(steps)}; }}"
    return start_command(f"{mount}/{DATA}", f"{mount}/{REPO}", heap, then), marker
