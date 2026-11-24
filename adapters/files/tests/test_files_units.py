# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The files adapter's parts that need no containers: trees, selection and checks."""

import datetime as dt
import shutil
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from datadouble.catalog import ColumnKind
from datadouble.engine import EngineError, Tooling, Transformation
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import PlannedRule, PlanOptions, SourceCatalog, plan
from datadouble.programs import Completed
from datadouble.runtime import Container, Server
from datadouble.spec import (
    FILES_SOURCE,
    ImageFormat,
    NullOptions,
    NullRule,
    TransformHook,
    parse_spec,
)
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    Edge,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)
from datadouble_files import FilesEngine, engine
from datadouble_files.source import FilesSnapshot, FilesSource
from datadouble_files.tree import CONTENT, PATH, catalog, chosen, find_tests, relative, table_name
from datadouble_files.working import FilesRestored, FilesWorking, text_runs

AS_OF = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
DOCS = table_name("documents")


def tree(root: Path) -> Path:
    """A small tree: two documents, an avatar with text in its bytes, and a log."""
    (root / "documents" / "1").mkdir(parents=True)
    (root / "documents" / "1" / "a.txt").write_text("hello world\n", "utf-8")
    (root / "documents" / "2.txt").write_text("call me at 617-222-3344\n", "utf-8")
    (root / "avatars").mkdir()
    (root / "avatars" / "1.jpg").write_bytes(b"\xff\xd8\x00Artist: Ann Example\x00\xff\xd9")
    (root / "tmp").mkdir()
    (root / "tmp" / "x.log").write_text("log", "utf-8")
    (root / "documents" / "link.txt").symlink_to(root / "tmp" / "x.log")
    return root


def test_catalog_has_a_table_per_top_level_directory(tmp_path: Path) -> None:
    found = catalog(tree(tmp_path))
    assert [str(t.name) for t in found.tables] == ["files.avatars", "files.documents", "files.tmp"]
    assert [c.name for c in found.tables[0].columns] == ["path", "size", "content"]
    (tmp_path / "loose.txt").write_text("x", "utf-8")
    with pytest.raises(EngineError, match="1 entries that are not directories"):
        catalog(tmp_path)
    with pytest.raises(EngineError, match="is not a directory"):
        catalog(tmp_path / "missing")


def test_find_tests_choose_regular_files(tmp_path: Path) -> None:
    root = tree(tmp_path)
    assert chosen(root, DOCS, AllRows(), AS_OF) == {"documents/1/a.txt", "documents/2.txt"}
    assert chosen(root, DOCS, Predicate("-name '2*'"), AS_OF) == {"documents/2.txt"}
    assert chosen(root, DOCS, Predicate("-newermt :as_of"), AS_OF) == {
        "documents/1/a.txt",
        "documents/2.txt",
    }
    assert chosen(root, DOCS, NoRows(), AS_OF) == set()
    assert chosen(root, DOCS, AnyNull((PATH,)), AS_OF) == set()
    for refused in ("-delete", "-exec rm {} ;", "-follow", "-fprint /tmp/x"):
        with pytest.raises(EngineError, match="is not allowed"):
            find_tests(refused, AS_OF)
    with pytest.raises(EngineError, match="cannot read the find tests"):
        find_tests("-name 'open", AS_OF)
    with pytest.raises(EngineError, match="find failed"):
        chosen(root, DOCS, Predicate("-bogus"), AS_OF)
    with pytest.raises(EngineError, match="keys strategy"):
        chosen(root, DOCS, SelectedBy((PATH,), DOCS, (PATH,)), AS_OF)


def test_listed_paths_stay_in_their_table(tmp_path: Path) -> None:
    root = tree(tmp_path)
    listed = ValuesIn(
        (PATH,),
        frozenset(
            {
                ("documents/2.txt",),
                ("/documents/1/a.txt",),
                ("./documents/1/./a.txt",),
                ("../documents/2.txt",),
                ("avatars/1.jpg",),
                ("documents/missing.txt",),
                ("documents/link.txt",),
                ("",),
            }
        ),
    )
    assert chosen(root, DOCS, listed, AS_OF) == {"documents/1/a.txt", "documents/2.txt"}
    both = AllOf((listed, Predicate("-name '2*'")))
    assert chosen(root, DOCS, both, AS_OF) == {"documents/2.txt"}
    assert chosen(root, DOCS, AnyOf(()), AS_OF) == set()
    assert chosen(root, DOCS, AnyOf((NoRows(), both)), AS_OF) == {"documents/2.txt"}
    assert relative("a/../b") is None
    with pytest.raises(EngineError, match="chosen by path, not size"):
        chosen(root, DOCS, ValuesIn((ColumnName("size"),), frozenset()), AS_OF)


def test_snapshots_need_consent(tmp_path: Path) -> None:
    source = FilesSource(tree(tmp_path), accept_inconsistent=False)
    assert source.server_version() == "1"
    assert source.catalog().tables
    # Every table of a tree sits in the one `files` namespace.
    assert source.namespaces() == ("files",)
    with pytest.raises(EngineError, match="accept_inconsistent: true"), source.snapshot(AS_OF):
        pass
    with FilesSource(tmp_path, accept_inconsistent=True).snapshot(AS_OF) as accepted:
        assert set(accepted.project(DOCS, NoRows(), (PATH,))) == set()
    snapshot = FilesSnapshot(tmp_path, AS_OF)
    assert set(snapshot.project(DOCS, Predicate("-name '2*'"), (PATH,))) == {("documents/2.txt",)}
    with pytest.raises(EngineError, match="chosen by path"):
        snapshot.project(DOCS, AllRows(), (ColumnName("size"),))


def test_restored_trees_read_as_text(tmp_path: Path) -> None:
    root = tree(tmp_path)
    restored = FilesRestored(root)
    assert restored.counts([DOCS, table_name("avatars")]) == {DOCS: 2, table_name("avatars"): 1}
    content = ColumnRef(table=table_name("avatars"), column=CONTENT)
    assert list(restored.text_values(content)) == ["Artist: Ann Example"]
    paths = ColumnRef(table=DOCS, column=PATH)
    assert sorted(restored.text_values(paths)) == ["documents/1/a.txt", "documents/2.txt"]
    assert list(restored.text_values(ColumnRef(table=DOCS, column=ColumnName("size")))) == []
    assert restored.failing_rows("-name '*.log'") == 1
    assert list(text_runs(root / "documents" / "2.txt")) == ["call me at 617-222-3344\n"[:-1]]
    edge = Edge(name="x", child=DOCS, columns=(PATH,), parent=DOCS, parent_columns=(PATH,))
    with pytest.raises(EngineError, match="no references"):
        restored.orphans(edge)


@dataclass
class _Runtime:
    """A container runtime that records commands and fails the ones that say so."""

    commands: list[str] = field(default_factory=list[str])

    def run(self, container: Container) -> Completed:
        self.commands.append(container.command[-1])
        return Completed(1 if "fail" in container.command[-1] else 0, "", "")

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        raise NotImplementedError

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def test_working_copies(tmp_path: Path) -> None:
    root = tree(tmp_path / "source")
    extract = tmp_path / "extract"
    (extract / "tree").mkdir(parents=True)
    (extract / "index.json").write_text('{"tables": []}', "utf-8")
    shutil.copytree(root / "documents", extract / "tree" / "documents", symlinks=True)
    runtime = _Runtime()
    working = FilesWorking(runtime, "img", tmp_path / "scratch")
    assert working.server_version() == "1"
    assert working.available() == ()
    planned = plan(
        parse_spec(
            "name: t\nsources: {files: {engine: files}}\nstrict: {data: off}\nexclude: [tmp]\n"
            "subset: {seeds: {documents: '-type f', avatars: '-type f'}}"
        ),
        (SourceCatalog(name=FILES_SOURCE, catalog=catalog(root), server_version="1"),),
        None,
        FilesEngine().capabilities(),
        PlanOptions(now=AS_OF),
    )
    group = planned.groups[0]
    counts = working.load(planned, group, (extract,), tmp_path / "loaded", ())
    assert {c.table: c.rows for c in counts} == {DOCS: 2, table_name("avatars"): 0}
    hook = TransformHook(sql="rm -f documents/2.txt")
    transformation = Transformation((), lambda _r, _v: {}, (hook,))
    working.transform(group, tmp_path / "loaded", tmp_path / "clean", transformation)
    assert runtime.commands[-1].endswith("&& rm -f documents/2.txt")
    failing = Transformation((), lambda _r, _v: {}, (TransformHook(sql="fail"),))
    with pytest.raises(EngineError, match="cleanup command 1 failed with status 1"):
        working.transform(group, tmp_path / "loaded", tmp_path / "bad", failing)
    rule = PlannedRule(
        column=ColumnRef(table=DOCS, column=CONTENT),
        kind=ColumnKind.TEXT,
        type="bytes",
        rule=NullRule(null=NullOptions()),
    )
    with pytest.raises(EngineError, match="no column rules"):
        working.transform(
            group,
            tmp_path / "loaded",
            tmp_path / "r",
            Transformation((rule,), lambda _r, _v: {}, ()),
        )
    with pytest.raises(EngineError, match="cannot copy"):
        working.transform(group, tmp_path / "nowhere", tmp_path / "n", transformation)
    image = tmp_path / "image"
    image.mkdir()
    working.package(tmp_path / "clean", ImageFormat.PHYSICAL, image)
    with working.restore(image, ImageFormat.PHYSICAL, tmp_path / "restored") as restored:
        assert restored.counts([DOCS]) == {DOCS: 2}
    for image_format in (ImageFormat.LOGICAL,):
        with pytest.raises(EngineError, match="only physical images"):
            working.package(tmp_path / "clean", image_format, tmp_path)
        with (
            pytest.raises(EngineError, match="only physical images"),
            working.restore(image, image_format, tmp_path / "r2"),
        ):
            pass


def test_the_engine(tmp_path: Path) -> None:
    files = FilesEngine()
    assert (files.name, engine().name) == ("files", "files")
    assert files.capabilities().complete
    assert files.owner
    with files.connect({"root": str(tree(tmp_path)), "accept_inconsistent": True}) as session:
        assert session.catalog().tables
    working = files.working(tmp_path, Tooling(runtime=_Runtime()), "1")
    assert isinstance(working, FilesWorking)
    with pytest.raises(EngineError, match="no server to start"):
        files.server(tmp_path, None)


def test_extract_copies_the_chosen_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tree(tmp_path / "source")
    planned = plan(
        parse_spec(
            "name: t\nsources: {files: {engine: files}}\nstrict: {data: off}\nexclude: [tmp]\n"
            "subset: {seeds: {documents: \"-name '2*'\", avatars: '-type f'}}"
        ),
        (SourceCatalog(name=FILES_SOURCE, catalog=catalog(root), server_version="1"),),
        None,
        FilesEngine().capabilities(),
        PlanOptions(now=AS_OF),
    )
    selections: dict[TableName, Condition] = {
        DOCS: AnyOf(
            (Predicate("-name '2*'"), ValuesIn((PATH,), frozenset({("documents/1/a.txt",)})))
        ),
        table_name("avatars"): AllRows(),
    }
    snapshot = FilesSnapshot(root, AS_OF)
    result = snapshot.extract(
        planned, selections, tmp_path / "extract", Tooling(runtime=_Runtime())
    )
    assert {str(c.table): c.rows for c in result.counts} == {
        "files.avatars": 1,
        "files.documents": 2,
    }
    copied = sorted(
        str(p.relative_to(tmp_path / "extract" / "tree"))
        for p in (tmp_path / "extract" / "tree").rglob("*")
        if p.is_file()
    )
    assert copied == ["avatars/1.jpg", "documents/1/a.txt", "documents/2.txt"]
    (root / "added").mkdir()
    with pytest.raises(EngineError, match="changed since planning"):
        snapshot.extract(planned, selections, tmp_path / "again", Tooling(runtime=_Runtime()))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(EngineError, match="GNU find was not found"):
        chosen(root, DOCS, AllRows(), AS_OF)
