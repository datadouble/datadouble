# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading Jailer subsets: a data model folder and an extraction model (``.jm``).

Both compile to the native subset model, so a subset designed in Jailer runs in
Datadouble unchanged, on every run (decision 81):

- each **subject** becomes a seed, its ``T.`` alias removed;
- each **association** is an equality join between two tables; one the
  catalog declares stays a foreign key, any other becomes a virtual one;
- Jailer follows every association in both directions unless restricted, so
  every child direction without a restriction becomes a followed child edge;
- ``ignore`` on a child direction leaves it unfollowed, and on a parent
  direction becomes ``on_missing: null`` (Jailer exports such rows with the
  reference dangling; the key sets agree);
- excluded tables and associations are excluded and ignored.

Anything that would change which rows are chosen and has no native
equivalent fails, naming it: non-equality joins, restriction conditions,
subject limits, column filters and ``${param}`` placeholders. Layout, XML and
export settings do not affect the rows and are listed in the report.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog, Table
from datadouble.spec import EdgePolicy, Follow, OnMissing, Subset, VirtualForeignKey

_ALIAS_T = re.compile(r"(?<![\w.\"`])T\.(?=[\w\"`])", re.IGNORECASE)
_PAIR = re.compile(r"^\s*(A|B)\.([\w$]+)\s*=\s*(A|B)\.([\w$]+)\s*$", re.IGNORECASE)
_AND = re.compile(r"\s+AND\s+", re.IGNORECASE)
_PARAMETER = re.compile(r"\$\{\w+\}")
_ASSOCIATION_CELLS = 6
"""A, B, first insert, cardinality, join condition, name."""
_LIMIT = 2
"""Where a subject's limit is: subject; condition; limit."""
_RESTRICTION = 2
"""Where an association's restriction is: association; ; restriction."""
_ROW_BLOCKS = frozenset({"", "additional subjects"})
_IGNORED_BLOCKS = frozenset(
    {
        "xml-mapping",
        "datamodelfolder",
        "export modus",
        "xml settings",
        "xml column mapping",
        "upserts",
        "exclude from deletion",
        "filter templates",
        "layout",
        "known",
        "version",
    }
)


class JailerError(Exception):
    """A Jailer model that cannot be read, or uses what Datadouble cannot run."""


@dataclass(frozen=True, slots=True)
class Association:
    """One row of ``association.csv``."""

    a: str
    b: str
    first_insert: str
    cardinality: str
    join: str
    name: str


@dataclass(frozen=True, slots=True)
class DataModel:
    """A Jailer data model folder."""

    tables: dict[str, tuple[str, ...]]
    """Table name to primary-key columns."""
    associations: tuple[Association, ...]
    excluded_tables: frozenset[str]
    excluded_associations: frozenset[str]


@dataclass(frozen=True, slots=True)
class Subject:
    """A starting table, its condition and optional limit."""

    table: str
    condition: str
    limit: str


@dataclass
class ExtractionModel:
    """The parts of a ``.jm`` file that choose rows."""

    subjects: list[Subject] = field(default_factory=list[Subject])
    restrictions: dict[str, str] = field(default_factory=dict[str, str])
    filters: list[str] = field(default_factory=list[str])
    ignored: list[str] = field(default_factory=list[str])


class Translation(BaseModel):
    """A Jailer subset in native terms, and what was left out on purpose."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    subset: Subset
    exclude: tuple[str, ...]
    report: tuple[str, ...]
    """Settings that do not choose rows and were ignored."""


def _rows(path: Path) -> list[list[str]]:
    """The semicolon-separated rows of a Jailer file, comments and blanks removed."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return [
            [cell.strip() for cell in row]
            for row in csv.reader(stream, delimiter=";")
            if row and row[0].strip() and not row[0].lstrip().startswith("#")
        ]


def read_datamodel(folder: Path) -> DataModel:
    """Read a data model folder's tables, associations and exclusions."""
    if not (folder / "table.csv").is_file():
        message = f"{folder} is not a Jailer data model folder (it has no table.csv)"
        raise JailerError(message)
    tables: dict[str, tuple[str, ...]] = {}
    for row in _rows(folder / "table.csv"):
        keys: list[str] = []
        for cell in row[2:]:
            if not cell:
                break
            keys.append(cell.split()[0])
        tables[row[0]] = tuple(keys)
    associations = tuple(
        Association(
            a=row[0], b=row[1], first_insert=row[2], cardinality=row[3], join=row[4], name=row[5]
        )
        for row in _rows(folder / "association.csv")
        if len(row) >= _ASSOCIATION_CELLS
    )
    return DataModel(
        tables=tables,
        associations=associations,
        excluded_tables=frozenset(row[0] for row in _rows(folder / "exclude-tables.csv")),
        excluded_associations=frozenset(
            row[0] for row in _rows(folder / "exclude-associations.csv")
        ),
    )


def read_extraction_model(path: Path) -> ExtractionModel:
    """Read a ``.jm`` file's subjects, restrictions and filters."""
    if not path.is_file():
        message = f"there is no Jailer extraction model at {path}"
        raise JailerError(message)
    model = ExtractionModel()
    block = ""
    section = 0
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#!"):
            block = line[2:].strip().removeprefix("block").strip().lower()
            continue
        if block == "" and line.startswith("# association"):
            section = 1
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in line.split(";")]
        if block in _ROW_BLOCKS and (block or section == 0):
            limit = cells[_LIMIT] if len(cells) > _LIMIT else ""
            model.subjects.append(Subject(cells[0], cells[1], limit))
        elif block == "" and len(cells) > _RESTRICTION:
            model.restrictions[cells[0]] = cells[_RESTRICTION]
        elif block == "filters":
            model.filters.append(line)
        elif block in _IGNORED_BLOCKS and block not in model.ignored:
            model.ignored.append(block)
    return model


@dataclass
class _Translator:
    datamodel: DataModel
    model: ExtractionModel
    catalog: Catalog
    case_sensitive: bool
    problems: list[str] = field(default_factory=list[str])

    def fold(self, name: str) -> str:
        return name if self.case_sensitive else name.lower()

    def table(self, jailer: str) -> Table | None:
        """The catalog table a Jailer table name means, with or without a schema."""
        wanted = self.fold(jailer)
        for table in self.catalog.tables:
            qualified = self.fold(f"{table.name.namespace}.{table.name.name}")
            if wanted in {qualified, self.fold(table.name.name)}:
                return table
        self.problems.append(f"table {jailer} is in the Jailer model but not in the catalog")
        return None

    def column(self, table: Table, jailer: str) -> str | None:
        for column in table.columns:
            if self.fold(column.name) == self.fold(jailer):
                return str(column.name)
        self.problems.append(
            f"column {table.name}.{jailer} is in the Jailer model but not in the catalog"
        )
        return None

    def check_keys(self) -> None:
        """The data model's primary keys must be the catalog's."""
        for name, keys in self.datamodel.tables.items():
            table = self.table(name)
            if table is None or not keys:
                continue
            if [self.fold(k) for k in keys] != [self.fold(k) for k in table.primary_key]:
                self.problems.append(
                    f"the primary key of {name} in the Jailer model ({', '.join(keys)}) "
                    f"is not the catalog's ({', '.join(table.primary_key)})"
                )

    def seeds(self) -> dict[str, str]:
        seeds: dict[str, list[str]] = {}
        for subject in self.model.subjects:
            table = self.table(subject.table)
            if subject.limit:
                self.problems.append(
                    f"subject {subject.table} has a limit, which Datadouble does not support yet"
                )
            if _PARAMETER.search(subject.condition):
                self.problems.append(f"subject {subject.table} uses a ${{param}} placeholder")
            if table is not None:
                condition = _strip_alias(subject.condition) or "1 = 1"
                seeds.setdefault(str(table.name), []).append(f"({condition})")
        return {name: " OR ".join(parts) for name, parts in seeds.items()}

    def edges(self) -> tuple[list[str], dict[str, EdgePolicy], dict[str, VirtualForeignKey]]:
        down: list[str] = []
        policies: dict[str, EdgePolicy] = {}
        virtual: dict[str, VirtualForeignKey] = {}
        for association in self.datamodel.associations:
            resolved = self.association(association)
            if resolved is None:
                continue
            child, child_columns, parent, parent_columns, upward, downward = resolved
            reference = (
                f"{child.name}.({', '.join(child_columns)}) -> "
                f"{parent.name}.({', '.join(parent_columns)})"
            )
            if not _declared(child, child_columns, parent):
                virtual[reference] = VirtualForeignKey()
            excluded = association.name in self.datamodel.excluded_associations
            up, below = (
                ("ignore", "ignore")
                if excluded
                else (
                    self.model.restrictions.get(upward, ""),
                    self.model.restrictions.get(downward, ""),
                )
            )
            for direction, restriction in ((upward, up), (downward, below)):
                if restriction and restriction.lower() != "ignore":
                    self.problems.append(
                        f"association {direction} has the restriction {restriction!r}; "
                        "restriction conditions are not supported yet"
                    )
            if up.lower() == "ignore":
                policies[reference] = EdgePolicy(on_missing=OnMissing.NULL)
            if not below:
                down.append(
                    f"{parent.name}.({', '.join(parent_columns)}) -> "
                    f"{child.name}.({', '.join(child_columns)})"
                )
        return down, policies, virtual

    def association(
        self, association: Association
    ) -> tuple[Table, list[str], Table, list[str], str, str] | None:
        """Child, its columns, parent, its columns, and the up and down direction names."""
        parts = _AND.split(association.join.strip())
        pairs = [match for part in parts if (match := _PAIR.match(part)) is not None]
        if len(pairs) != len(parts):
            self.problems.append(
                f"association {association.name} joins on {association.join!r}; "
                "only equality between columns is supported"
            )
            return None
        if association.first_insert.upper() == "B" or (
            not association.first_insert and association.cardinality == "n:1"
        ):
            child_side, upward, downward = "A", association.name, f"inverse-{association.name}"
        elif association.first_insert.upper() == "A" or association.cardinality == "1:n":
            child_side, upward, downward = "B", f"inverse-{association.name}", association.name
        else:
            self.problems.append(
                f"association {association.name} has no direction "
                "(no first-insert side and cardinality is not n:1 or 1:n)"
            )
            return None
        child = self.table(association.a if child_side == "A" else association.b)
        parent = self.table(association.b if child_side == "A" else association.a)
        if child is None or parent is None:
            return None
        child_columns: list[str] = []
        parent_columns: list[str] = []
        for match in pairs:
            sides = {match.group(1).upper(): match.group(2), match.group(3).upper(): match.group(4)}
            if set(sides) != {"A", "B"}:
                self.problems.append(f"association {association.name} compares a table with itself")
                return None
            mine = self.column(child, sides[child_side])
            theirs = self.column(parent, sides["B" if child_side == "A" else "A"])
            if mine is None or theirs is None:
                return None
            child_columns.append(mine)
            parent_columns.append(theirs)
        return child, child_columns, parent, parent_columns, upward, downward


def _strip_alias(condition: str) -> str:
    """``condition`` without Jailer's ``T.`` alias, outside quoted strings."""
    parts = re.split(r"('(?:[^']|'')*')", condition)
    return "".join(
        part if i % 2 else _ALIAS_T.sub("", part) for i, part in enumerate(parts)
    ).strip()


def _declared(child: Table, columns: Iterable[str], parent: Table) -> bool:
    wanted = tuple(columns)
    return any(
        fk.target == parent.name and tuple(fk.columns) == wanted for fk in child.foreign_keys
    )


def translate(
    datamodel: DataModel,
    model: ExtractionModel,
    catalog: Catalog,
    *,
    case_sensitive: bool = False,
) -> Translation:
    """The native subset for a Jailer subset over ``catalog``; raises listing every problem."""
    translator = _Translator(datamodel, model, catalog, case_sensitive)
    translator.check_keys()
    seeds = translator.seeds()
    down, policies, virtual = translator.edges()
    if model.filters:
        translator.problems.append("column filters are not supported; use anonymization rules")
    if translator.problems:
        raise JailerError("; ".join(dict.fromkeys(translator.problems)))
    excluded = sorted(
        str(table.name)
        for name in datamodel.excluded_tables
        if (table := translator.table(name)) is not None
    )
    subset = Subset(
        seeds=seeds, follow=Follow(down=tuple(down)), edges=policies, virtual_fks=virtual
    )
    report = tuple(
        f"ignored the {block} block, which does not choose rows" for block in model.ignored
    )
    return Translation(subset=subset, exclude=tuple(excluded), report=report)


def load(
    model: Path, datamodel: Path, catalog: Catalog, *, case_sensitive: bool = False
) -> Translation:
    """Read and translate a Jailer subset."""
    return translate(
        read_datamodel(datamodel),
        read_extraction_model(model),
        catalog,
        case_sensitive=case_sensitive,
    )
