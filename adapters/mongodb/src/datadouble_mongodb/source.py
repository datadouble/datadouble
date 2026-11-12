# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: a sampled catalog and one snapshot session.

MongoDB collections declare no columns, so the catalog's columns are the field
paths (nested documents flattened to dotted paths, arrays kept whole) declared
by a ``$jsonSchema`` validator, if the collection has one, followed by any
others seen in its first ``sample`` documents in natural order: a field the
validator does not declare is still noticed. Every
collection's key is ``_id``; references are declared as virtual foreign keys.

Every read goes through one snapshot session, so the whole extract sees one
point in time. Documents are copied as raw BSON into ``mongodump``'s layout,
with each collection's options and indexes, for ``mongorestore`` to load.
"""

import datetime as dt
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import bson
from bson import Binary, Decimal128, Int64, ObjectId, json_util
from bson.codec_options import CodecOptions
from bson.raw_bson import RawBSONDocument
from pydantic import BaseModel, ConfigDict
from pymongo import MongoClient
from pymongo.client_session import ClientSession
from pymongo.database import Database
from pymongo.errors import PyMongoError

from datadouble.catalog import Catalog, Column, ColumnKind, Table
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_mongodb.filters import as_document, as_list, keys, query

ENGINE = EngineName("mongodb")

type Client = MongoClient[dict[str, object]]

_KINDS: tuple[tuple[type, ColumnKind, str], ...] = (
    (bool, ColumnKind.BOOLEAN, "bool"),
    (int, ColumnKind.INTEGER, "int"),
    (Int64, ColumnKind.INTEGER, "long"),
    (float, ColumnKind.FLOAT, "double"),
    (Decimal128, ColumnKind.DECIMAL, "decimal"),
    (Decimal, ColumnKind.DECIMAL, "decimal"),
    (str, ColumnKind.TEXT, "string"),
    (dt.datetime, ColumnKind.TIMESTAMP, "date"),
    (ObjectId, ColumnKind.OTHER, "objectId"),
    (uuid.UUID, ColumnKind.UUID, "uuid"),
    (Binary, ColumnKind.BINARY, "binData"),
    (bytes, ColumnKind.BINARY, "binData"),
    (list, ColumnKind.ARRAY, "array"),
    (dict, ColumnKind.JSON, "object"),
)
_SCHEMA_KINDS = {name: kind for _, kind, name in _KINDS}


def failure(error: PyMongoError, action: str) -> EngineError:
    """An :class:`EngineError` naming the server's error code, not documents."""
    code = getattr(error, "code", None)
    name = type(error).__name__
    return EngineError(f"{action} failed: {name}" + (f" (code {code})" if code else ""))


def _type(value: object) -> tuple[ColumnKind, str]:
    for kind_type, kind, name in _KINDS:
        if isinstance(value, kind_type):
            return kind, name
    return ColumnKind.OTHER, type(value).__name__


def _flatten(document: Mapping[str, object], prefix: str = "") -> Iterable[tuple[str, object]]:
    for key, value in document.items():
        path = f"{prefix}{key}"
        nested = as_document(value)
        if nested:
            yield from _flatten(nested, f"{path}.")
        else:
            yield path, value


def sampled_columns(documents: Iterable[Mapping[str, object]]) -> tuple[Column, ...]:
    """Columns for the field paths seen in ``documents``."""
    seen: defaultdict[str, set[tuple[ColumnKind, str]]] = defaultdict(set)
    present: set[str] = set()
    for document in documents:
        for path, value in _flatten(document):
            present.add(path)
            if value is not None:
                seen[path].add(_type(value))
    order = sorted(present, key=lambda path: (path != "_id", path))
    columns: list[Column] = []
    for path in order:
        kinds = {kind for kind, _ in seen[path]}
        columns.append(
            Column(
                name=ColumnName(path),
                type="|".join(sorted({name for _, name in seen[path]})) or "null",
                kind=kinds.pop() if len(kinds) == 1 else ColumnKind.OTHER,
                # Without a validator, any field but _id can be null or missing.
                nullable=path != "_id",
            )
        )
    return tuple(columns)


def schema_columns(schema: Mapping[str, object]) -> tuple[Column, ...] | None:
    """Columns for a ``$jsonSchema``'s properties, or None if it declares none."""
    properties = as_document(schema.get("properties"))
    if properties is None:
        return None
    required = {str(item) for item in as_list(schema.get("required")) or []}
    columns: list[Column] = []
    for name in sorted(properties, key=lambda path: (path != "_id", path)):
        spec = as_document(properties[name]) or {}
        bson_type = spec.get("bsonType", "object")
        names = as_list(bson_type)
        type_name = "|".join(map(str, names)) if names is not None else str(bson_type)
        columns.append(
            Column(
                name=ColumnName(name),
                type=type_name,
                kind=_SCHEMA_KINDS.get(type_name, ColumnKind.OTHER),
                nullable=name not in required and name != "_id",
            )
        )
    return tuple(columns)


def introspect(database: Database[dict[str, object]], sample: int) -> Catalog:
    """Read the catalog: one table per collection, columns from sampling or a validator."""
    tables: list[Table] = []
    ordinary = {"type": "collection", "name": {"$not": {"$regex": r"^system\."}}}
    for info in database.list_collections(filter=ordinary):
        name = str(info["name"])
        options = as_document(info.get("options")) or {}
        validator = as_document(options.get("validator")) or {}
        schema = as_document(validator.get("$jsonSchema"))
        declared = (schema_columns(schema) if schema is not None else None) or ()
        sampled = sampled_columns(database[name].find({}, limit=sample))
        known = {column.name for column in declared}
        columns = (*declared, *(column for column in sampled if column.name not in known))
        tables.append(
            Table(
                name=TableName.of(database.name, name),
                columns=columns,
                primary_key=(ColumnName("_id"),),
            )
        )
    tables.sort(key=lambda table: table.name.name)
    return Catalog(engine=ENGINE, tables=tuple(tables))


class ExtractedCollection(BaseModel):
    """One collection in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    documents: int


class ExtractIndex(BaseModel):
    """What an extract directory holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    collections: tuple[ExtractedCollection, ...]


class MongoSnapshot:
    """A snapshot session on the source."""

    def __init__(self, source: "MongoSource", session: ClientSession, as_of: dt.datetime) -> None:
        """Read through ``session``."""
        self.source = source
        self.session = session
        self.as_of = as_of
        self.opened = time.monotonic()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen documents."""
        collection = self.source.database[table.name]
        projection = dict.fromkeys(columns, 1)
        found: set[KeyTuple] = set()
        try:
            for document in collection.find(
                query(condition, self.as_of), projection, session=self.session
            ):
                found.update(keys(document, columns))
        except PyMongoError as error:
            raise failure(error, f"reading keys of {table}") from None
        return found

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,  # noqa: ARG002 - extract reads through the driver, without tools
    ) -> ExtractResult:
        """Write the chosen documents, options and indexes in mongodump's layout."""
        database = self.source.database
        # Snapshot sessions run only reads such as find and aggregate, so the
        # collections, their options and indexes are listed outside it.
        current = introspect(database, self.source.sample)
        if current.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source's collections or sampled fields changed between planning and "
                "the snapshot; plan again (start a new generation)"
            )
            raise EngineError(message)
        target = directory / database.name
        target.mkdir(parents=True, exist_ok=True)
        raw: CodecOptions[RawBSONDocument] = CodecOptions(document_class=RawBSONDocument)
        raw_database = self.source.client.get_database(database.name, codec_options=raw)
        extracted: list[ExtractedCollection] = []
        try:
            infos = {
                str(info["name"]): info
                for info in database.list_collections(filter={"type": "collection"})
            }
            for table in plan.extracted():
                collection = raw_database[table.name]
                count = 0
                with (target / f"{table.name}.bson").open("wb") as out:
                    for document in collection.find(
                        query(selections[table], self.as_of), session=self.session
                    ):
                        out.write(bson.encode(document))
                        count += 1
                metadata = {
                    "options": infos.get(table.name, {}).get("options", {}),
                    "indexes": list(database[table.name].list_indexes()),
                    "collectionName": table.name,
                    "type": "collection",
                }
                (target / f"{table.name}.metadata.json").write_text(
                    json_util.dumps(metadata, json_options=json_util.CANONICAL_JSON_OPTIONS),
                    "utf-8",
                )
                extracted.append(ExtractedCollection(table=table, documents=count))
            version = str(self.source.client.server_info().get("version", ""))
        except PyMongoError as error:
            raise failure(error, "extracting") from None
        index = ExtractIndex(database=database.name, collections=tuple(extracted))
        (directory / "index.json").write_text(index.model_dump_json(indent=2), "utf-8")
        snapshot_time = self.session.operation_time
        return ExtractResult(
            server_version=version,
            snapshot=json.dumps({"t": snapshot_time.time, "i": snapshot_time.inc})
            if snapshot_time
            else "",
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=c.table, rows=c.documents) for c in extracted),
        )


class MongoSource:
    """An open connection to the source database."""

    def __init__(self, client: Client, database: str, sample: int) -> None:
        """Wrap a client."""
        self.client = client
        self.database: Database[dict[str, object]] = client[database]
        self.sample = sample

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.database, self.sample)
        except PyMongoError as error:
            raise failure(error, "reading the catalog") from None

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open a snapshot session; every read inside it sees one point in time."""
        try:
            with self.client.start_session(snapshot=True, causal_consistency=False) as session:
                yield MongoSnapshot(self, session, as_of)
        except PyMongoError as error:
            raise failure(error, "reading the snapshot") from None
