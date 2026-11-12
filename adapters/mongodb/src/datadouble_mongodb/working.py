# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` restores the group's collections from the extract into a fresh
``mongod`` with ``mongorestore`` (which also builds the indexes) and applies
the fallbacks for missing parents. ``transform`` copies the stopped data
directory and anonymizes the copy, so an interrupted transform leaves the
loaded database untouched. ``package`` writes the data directory (physical) or
a ``mongodump`` archive (logical); ``restore`` brings either back up.
"""

import datetime as dt
import shutil
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pymongo import UpdateMany
from pymongo.database import Database
from pymongo.errors import PyMongoError

from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_mongodb.filters import Document, as_document, as_list, parse
from datadouble_mongodb.server import MongoServer, Tools
from datadouble_mongodb.source import ExtractIndex, failure

_DBPATH = "db"
_ARCHIVE = "database.archive"
_INFO = "database.json"
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    server_version: str


def _single(edge: Edge) -> tuple[str, str]:
    if len(edge.columns) != 1:
        message = f"{edge.name}: MongoDB references must have one field on each side"
        raise EngineError(message)
    return edge.columns[0], edge.parent_columns[0]


class MongoRestored:
    """A database open for checks: a restored image, or a working database."""

    def __init__(self, database: Database[dict[str, object]]) -> None:
        """Wrap a database."""
        self.database = database

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Documents per collection."""
        try:
            return {table: self.database[table.name].count_documents({}) for table in tables}
        except PyMongoError as error:
            raise failure(error, "counting") from None

    def orphans(self, edge: Edge) -> int:
        """Child documents with a reference (or array element) that has no parent."""
        child, parent = _single(edge)
        when: Document = parse(edge.when, _EPOCH, f"{edge.name} `when`") if edge.when else {}
        try:
            parents = self.database[edge.parent.name].distinct(parent)
            pipeline: list[Document] = [
                {"$match": {**when, child: {"$ne": None}}},
                {"$unwind": f"${child}"},
                {"$match": {child: {"$nin": parents}}},
                {"$group": {"_id": "$_id"}},
                {"$count": "orphans"},
            ]
            found = list(self.database[edge.child.name].aggregate(pipeline))
        except PyMongoError as error:
            raise failure(error, f"checking {edge.name}") from None
        return int(str(found[0]["orphans"])) if found else 0

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct string values at a field path, streamed."""
        pipeline: list[Document] = [
            {"$match": {column.column: {"$type": "string"}}},
            {"$group": {"_id": f"${column.column}"}},
        ]
        try:
            for document in self.database[column.table.name].aggregate(pipeline, allowDiskUse=True):
                yield str(document["_id"])
        except PyMongoError as error:
            raise failure(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Documents an assertion returns: ``{"collection": ..., "pipeline": [...]}``."""
        assertion = parse(query, _EPOCH, "an assertion")
        collection = assertion.get("collection")
        pipeline = as_list(assertion.get("pipeline"))
        if not isinstance(collection, str) or pipeline is None:
            message = 'a MongoDB assertion is {"collection": "...", "pipeline": [...]}'
            raise EngineError(message)
        try:
            stages = [stage for item in pipeline if (stage := as_document(item)) is not None]
            return len(list(self.database[collection].aggregate(stages)))
        except PyMongoError as error:
            raise failure(error, "running an assertion") from None


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


def apply_fixup(database: Database[dict[str, object]], fixup: Fixup) -> None:
    """Null references to missing parents, or insert stub parents for them."""
    edge = fixup.edge
    child, parent = _single(edge)
    when: Document = parse(edge.when, _EPOCH, f"{edge.name} `when`") if edge.when else {}
    try:
        present = database[edge.parent.name].distinct(parent) if fixup.parent_present else []
        missing: Document = {**when, child: {"$ne": None, "$nin": present}}
        if fixup.mode is EdgeMode.NULL:
            database[edge.child.name].update_many(missing, {"$set": {child: None}})
            return
        values = database[edge.child.name].distinct(child, missing)
        for value in values:
            database[edge.parent.name].update_one(
                {parent: value}, {"$setOnInsert": {parent: value}}, upsert=True
            )
    except PyMongoError as error:
        raise failure(error, f"applying the fallback for {edge.name}") from None


class MongoWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every server under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's ``mongod``."""
        if self._version is None:
            self._version = self.tools.version(self.scratch)
        return self._version

    def load(
        self,
        plan: Plan,  # noqa: ARG002 - the protocol passes the plan; restoring needs only the group
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Restore the group's collections and apply its fallbacks."""
        if hooks:
            message = "the mongodb adapter does not run hooks"
            raise EngineError(message)
        index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
        members = set(group.tables)
        server = MongoServer(self.tools, directory / _DBPATH, directory, [extract])
        with server.running(), server.client() as client:
            includes = [
                f"--nsInclude={index.database}.{c.table.name}"
                for c in index.collections
                if c.table in members
            ]
            if includes:
                server.tool("mongorestore", [*includes, str(extract)])
            database = client[index.database]
            for fixup in group.fixups:
                apply_fixup(database, fixup)
            counts = MongoRestored(database).counts(group.tables)
        info = DatabaseInfo(database=index.database, server_version=self.server_version())
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
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
            message = "the mongodb adapter does not run hooks"
            raise EngineError(message)
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        server = MongoServer(self.tools, directory / _DBPATH, directory)
        with server.running(), server.client() as client:
            database = client[info.database]
            for rule in transformation.rules:
                self._anonymize(database, rule, transformation)
            counts = MongoRestored(database).counts(group.tables)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _anonymize(
        database: Database[dict[str, object]], rule: PlannedRule, transformation: Transformation
    ) -> None:
        collection = database[rule.column.table.name]
        field = rule.column.column
        try:
            match rule.rule:
                case NullRule():
                    collection.update_many({field: {"$ne": None}}, {"$set": {field: None}})
                case ExpressionRule(expression=expression, where=where):
                    value = parse(
                        f'{{"value": {expression}}}', _EPOCH, f"the expression for {rule.column}"
                    )
                    match_: Document = (
                        parse(where, _EPOCH, f"the `where` for {rule.column}") if where else {}
                    )
                    collection.update_many(match_, [{"$set": {field: value["value"]}}])
                case _:
                    originals = [v for v in collection.distinct(field) if isinstance(v, str | int)]
                    mapping = transformation.mapper(rule, [str(v) for v in originals])
                    updates = [
                        UpdateMany(
                            {field: original},
                            {"$set": {field: _typed(original, mapping[str(original)])}},
                        )
                        for original in originals
                    ]
                    if updates:
                        collection.bulk_write(updates, ordered=False)
        except PyMongoError as error:
            raise failure(error, f"anonymizing {rule.column}") from None

    # package and restore

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        shutil.copyfile(source / _INFO, directory / _INFO)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(source / _DBPATH, directory / _DBPATH)
            return
        info = DatabaseInfo.model_validate_json((source / _INFO).read_bytes())
        server = MongoServer(self.tools, source / _DBPATH, directory, [directory])
        with server.running():
            server.tool(
                "mongodump",
                [f"--db={info.database}", f"--archive={directory / _ARCHIVE}", "--gzip"],
            )

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch server for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        directory.mkdir(parents=True, exist_ok=True)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / _DBPATH, directory / _DBPATH)
        server = MongoServer(self.tools, directory / _DBPATH, directory, [image])
        with server.running(), server.client() as client:
            if image_format is ImageFormat.LOGICAL:
                server.tool("mongorestore", [f"--archive={image / _ARCHIVE}", "--gzip"])
            yield MongoRestored(client[info.database])


def _typed(original: object, replacement: str) -> object:
    """Keep integer fields integers when a hash replaced them."""
    if isinstance(original, int) and not isinstance(original, bool) and replacement.isdigit():
        return int(replacement)
    return replacement
