# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A spec's files beside a PostgreSQL source, in the Compose postgres service.

The tree is generated for each test: avatars and documents keyed by customer
ID, with fake personal data in a photo's metadata and in one document.
"""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from psycopg import sql

from datadouble.catalog import CatalogLock
from datadouble.planner import PlanError, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.spec import parse_spec
from datadouble.storage import filesystem_store
from datadouble_conformance.engine import runtime_for_tests
from datadouble_files import FilesEngine
from datadouble_postgres import PostgresEngine
from test_conformance import PostgresScratch, scratch

pytestmark = pytest.mark.integration

PLANTED = "Reach me at jane.roe@realmail.net"
ENGINES = ("postgres-", "files-")
TEMPLATE = "{table: customer_document, path: 'documents/{customer_id}/{file_name}'}"
URL = (
    "{table: customer_document, path: '{url}',"
    " match: '^https://cdn[.]example[.]com/uploads/([^?]+)'}"
)

SPEC = """
name: uploads
source: {engine: postgres, connection: CONNECTION}
subset:
  seeds: {customer: "id <= 3"}
  follow: {down: ["customer -> customer_document"]}
  unselected: none
files:
  connection: {root: ROOT, accept_inconsistent: true}
  select: {avatars: "-name '1.jpg'", documents: -false}
  exclude: [EXCLUDED]
  paths_from: PATHS_FROM
  cleanup: CLEANUP
store:
  - filesystem: {path: STORE}
"""


def jpeg(text: str) -> bytes:
    """A JPEG with an EXIF segment holding ``text``, as cameras and apps write them."""
    exif = b"Exif\x00\x00II*\x00" + text.encode() + b"\x00"
    segment = b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
    return b"\xff\xd8" + segment + b"\xff\xd9"


def make_tree(root: Path) -> Path:
    """Avatars and documents for customers 1 to 5, a temporary directory and a cache."""
    for customer in range(1, 6):
        (root / "avatars").mkdir(parents=True, exist_ok=True)
        (root / "avatars" / f"{customer}.jpg").write_bytes(jpeg(f"Customer {customer}"))
        folder = root / "documents" / str(customer)
        folder.mkdir(parents=True)
        (folder / "lease.txt").write_text(f"Lease for unit {customer}\n", "utf-8")
    (root / "documents" / "3" / "contact.txt").write_text(PLANTED + "\n", "utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / "upload.part").write_text("partial", "utf-8")
    (root / "cache").mkdir()
    (root / "documents" / "1" / "render.log").write_text("rendered", "utf-8")
    return root


@pytest.fixture
def source() -> Generator[PostgresScratch]:
    with scratch() as made:
        assert isinstance(made, PostgresScratch)
        made.execute(sql.SQL("CREATE TABLE customer (id integer PRIMARY KEY)"))
        # Documents are stored by URL, as an app serving them from a CDN would.
        made.execute(
            sql.SQL(
                "CREATE TABLE customer_document (id serial PRIMARY KEY,"
                " customer_id integer NOT NULL REFERENCES customer, file_name text NOT NULL,"
                " url text)"
            )
        )
        made.execute(sql.SQL("INSERT INTO customer SELECT generate_series(1, 5)"))
        made.execute(
            sql.SQL(
                "INSERT INTO customer_document (customer_id, file_name)"
                " SELECT n, 'lease.txt' FROM generate_series(1, 5) n"
                " UNION ALL VALUES (3, 'contact.txt'), (2, '../../../etc/passwd'),"
                " (1, 'render.log'), (2, 'missing.txt')"
            )
        )
        made.execute(
            sql.SQL(
                "UPDATE customer_document SET url = 'https://cdn.example.com/uploads/documents/'"
                " || customer_id || '/' || file_name || '?v=' || id"
            )
        )
        made.execute(sql.SQL("UPDATE customer_document SET url = 'mailto:x' WHERE id = 2"))
        yield made


def build(
    source: PostgresScratch,
    tmp_path: Path,
    *,
    excluded: str = "tmp, cache",
    paths_from: str = TEMPLATE,
    cleanup: tuple[str, ...] = ("find . -name '*.log' -delete",),
    stages: tuple[Stage, ...] = tuple(Stage),
) -> RunResult:
    root = tmp_path / "uploads"
    if not root.exists():
        make_tree(root)
    text = (
        SPEC.replace("CONNECTION", json.dumps(dict(source.connection)))
        .replace("ROOT", str(root))
        .replace("EXCLUDED", excluded)
        .replace("PATHS_FROM", paths_from)
        .replace("CLEANUP", json.dumps(list(cleanup)))
        .replace("STORE", str(tmp_path / "store"))
    )
    spec = parse_spec(text + f"runtime: {json.dumps(runtime_for_tests())}\n")
    with PostgresEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    registry = Registry(
        engines={"postgres": PostgresEngine, "files": FilesEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )
    options = RunOptions(stages=stages, work=tmp_path / "work", plan=PlanOptions())
    return Run(spec, lock, registry, options).execute()


def _engine(key: str) -> str:
    """The engine a stored file belongs to: ``.../uploads/all/files-1/...`` is ``files``."""
    return next(part.split("-")[0] for part in Path(key).parts if part.startswith(ENGINES))


def copied(result: RunResult) -> list[str]:
    tree = Path(result.work) / "files" / "extract" / "data" / "tree"
    return sorted(str(path.relative_to(tree)) for path in tree.rglob("*") if path.is_file())


def test_files_follow_the_chosen_rows(source: PostgresScratch, tmp_path: Path) -> None:
    """Only the subset's files are copied, cleaned, and checked for personal data."""
    result = build(source, tmp_path)
    # Customers 1 to 3 and the avatar the seed selects; the path outside the
    # tree and the missing file are skipped.
    assert copied(result) == [
        "avatars/1.jpg",
        "documents/1/lease.txt",
        "documents/1/render.log",
        "documents/2/lease.txt",
        "documents/3/contact.txt",
        "documents/3/lease.txt",
    ]
    cleaned = Path(result.work) / "files" / "transform" / "all" / "db" / "tree"
    assert not (cleaned / "documents" / "1" / "render.log").exists()
    # The planted document fails the files' PII scan; the database's image passes.
    by_engine = {s.identity.engine: s.verification.passed for s in result.images}
    assert by_engine == {"postgres": True, "files": False}
    failing = [c for r in result.verification for c in r.checks() if not c.passed]
    assert any(c.name == "files.documents.content" for c in failing), failing
    assert {_engine(key) for key in result.stored} == {"postgres"}


def test_cleanup_makes_the_files_pass(source: PostgresScratch, tmp_path: Path) -> None:
    """Paths can come from URLs; a value the pattern does not match is reported."""
    result = build(source, tmp_path, cleanup=("rm documents/3/contact.txt",), paths_from=URL)
    assert "documents/3/contact.txt" in copied(result)
    assert "documents/2/lease.txt" not in copied(result)
    assert [w for w in result.warnings if "did not match" in w]
    assert not result.failed(), result.verification
    assert {s.identity.engine for s in result.images} == {"postgres", "files"}
    assert {_engine(key) for key in result.stored} == {"postgres", "files"}


def test_every_top_level_directory_is_accounted_for(
    source: PostgresScratch, tmp_path: Path
) -> None:
    with pytest.raises(PlanError) as caught:
        build(source, tmp_path, excluded="tmp", stages=(Stage.PLAN,))
    assert caught.value.problems == ("files.cache is neither selected nor excluded",)
