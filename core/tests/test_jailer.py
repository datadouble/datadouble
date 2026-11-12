# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from datadouble import jailer
from datadouble.catalog import CatalogLock
from datadouble.planner import Capabilities, PlanError, PlanOptions, plan
from datadouble.spec import (
    EdgePolicy,
    ImageFormat,
    OnMissing,
    SpecError,
    VirtualForeignKey,
    parse_spec,
)
from fakes import shop

TABLES = """# Name; Upsert; Primary Key; ; Author
USERS; N; ID INTEGER; ; Jailer
CATEGORIES; N; ID INTEGER; ; Jailer
POSTS; N; ID INTEGER; ; Jailer
COMMENTS; N; ID INTEGER; ; Jailer
PUBLIC.EVENTS; N; ID INTEGER; ; Jailer
"""

ASSOCIATIONS = """# Table A; Table B; First-insert; Cardinality (opt); Join-condition; Name; Author
POSTS; USERS; B; n:1; A.USER_ID=B.ID; POSTS_USER; Jailer
POSTS; CATEGORIES; ; n:1; A.CATEGORY_ID = B.ID; POSTS_CATEGORY; Jailer
COMMENTS; POSTS; B; n:1; A.POST_ID=B.ID; COMMENTS_POST; Jailer
COMMENTS; USERS; B; n:1; A.AUTHOR_ID=B.ID; COMMENTS_AUTHOR; Jailer
POSTS; EVENTS; A; 1:n; B.SUBJECT_ID=A.ID; EVENTS_POST; Jailer
"""

MODEL = """# subject; condition; limit; limit-order
USERS; T.id <= 2 AND 'T.x' <> T.name; ;

# association; ; restriction-condition
inverse-COMMENTS_POST; ; ignore
POSTS_CATEGORY; ; ignore

#! block additional subjects
# subject; condition; limit; limit-order
USERS; T.id = 9; ;
PUBLIC.EVENTS; ; ;

#! block filters

#! block layout
USERS; POSTS; 1.0; 2.0; 1.0
USERS; USERS; 0.0; 0.0; 1.0

#! block version
17.3.1
"""


def model_files(
    root: Path, *, model: str = MODEL, associations: str = ASSOCIATIONS, tables: str = TABLES
) -> tuple[Path, Path]:
    datamodel = root / "datamodel"
    datamodel.mkdir(parents=True, exist_ok=True)
    (datamodel / "table.csv").write_text(tables, "utf-8")
    (datamodel / "association.csv").write_text(associations, "utf-8")
    (datamodel / "exclude-tables.csv").write_text("# excluded\nCATEGORIES\n", "utf-8")
    (datamodel / "exclude-associations.csv").write_text("COMMENTS_AUTHOR\n", "utf-8")
    jm = root / "model.jm"
    jm.write_text(model, "utf-8")
    return jm, datamodel


def test_translation(tmp_path: Path) -> None:
    jm, datamodel = model_files(tmp_path)
    translated = jailer.load(jm, datamodel, shop())
    subset = translated.subset
    assert subset.seeds == {
        "public.users": "(id <= 2 AND 'T.x' <> name) OR (id = 9)",
        "public.events": "(1 = 1)",
    }
    assert subset.follow.down == (
        "public.users.(id) -> public.posts.(user_id)",
        "public.categories.(id) -> public.posts.(category_id)",
        "public.posts.(id) -> public.events.(subject_id)",
    )
    assert subset.edges == {
        "public.posts.(category_id) -> public.categories.(id)": EdgePolicy(
            on_missing=OnMissing.NULL
        ),
        "public.comments.(author_id) -> public.users.(id)": EdgePolicy(on_missing=OnMissing.NULL),
    }
    assert subset.virtual_fks == {
        "public.events.(subject_id) -> public.posts.(id)": VirtualForeignKey()
    }
    assert translated.exclude == ("public.categories",)
    assert translated.report == (
        "ignored the layout block, which does not choose rows",
        "ignored the version block, which does not choose rows",
    )


@pytest.mark.parametrize(
    ("model", "associations", "tables", "problem"),
    [
        (MODEL, ASSOCIATIONS.replace("A.USER_ID=B.ID", "A.USER_ID>B.ID"), TABLES, "only equality"),
        (MODEL, ASSOCIATIONS.replace("B; n:1; A.POST_ID", "; ; A.POST_ID"), TABLES, "no direction"),
        (MODEL, ASSOCIATIONS.replace("A.POST_ID=B.ID", "A.POST_ID=A.ID"), TABLES, "with itself"),
        (
            MODEL,
            ASSOCIATIONS.replace("A.POST_ID=B.ID", "A.NOPE=B.ID"),
            TABLES,
            "public.comments.NOPE",
        ),
        (
            MODEL,
            ASSOCIATIONS + "NOWHERE; USERS; B; n:1; A.ID=B.ID; NOWHERE_USER\n",
            TABLES,
            "NOWHERE",
        ),
        (
            MODEL,
            ASSOCIATIONS,
            TABLES.replace("USERS; N; ID", "USERS; N; EMAIL"),
            "primary key of USERS",
        ),
        (MODEL.replace("T.id = 9; ;", "T.id = 9; 10;"), ASSOCIATIONS, TABLES, "has a limit"),
        (MODEL.replace("T.id = 9", "T.id = ${who}"), ASSOCIATIONS, TABLES, "placeholder"),
        (
            MODEL.replace("POSTS_CATEGORY; ; ignore", "POSTS_CATEGORY; ; B.id > 1"),
            ASSOCIATIONS,
            TABLES,
            "restriction conditions",
        ),
        (
            MODEL.replace("#! block filters\n", "#! block filters\nUSERS; NAME; 'x'\n"),
            ASSOCIATIONS,
            TABLES,
            "column filters",
        ),
    ],
)
def test_unsupported_models(
    tmp_path: Path, model: str, associations: str, tables: str, problem: str
) -> None:
    jm, datamodel = model_files(tmp_path, model=model, associations=associations, tables=tables)
    with pytest.raises(jailer.JailerError, match=problem):
        jailer.load(jm, datamodel, shop())


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(jailer.JailerError, match="not a Jailer data model folder"):
        jailer.read_datamodel(tmp_path)
    with pytest.raises(jailer.JailerError, match="no Jailer extraction model"):
        jailer.read_extraction_model(tmp_path / "missing.jm")
    (tmp_path / "table.csv").write_text("USERS; N; ID INTEGER\n", "utf-8")
    model = jailer.read_datamodel(tmp_path)
    assert model.associations == ()
    assert model.excluded_tables == frozenset()


def test_case_sensitive_names(tmp_path: Path) -> None:
    jm, datamodel = model_files(tmp_path)
    with pytest.raises(
        jailer.JailerError, match="table USERS is in the Jailer model but not in the catalog"
    ):
        jailer.load(jm, datamodel, shop(), case_sensitive=True)


HEADER = "name: shop\nsource: {engine: fake}\n"


def test_planning_a_jailer_subset(tmp_path: Path) -> None:
    jm, datamodel = model_files(tmp_path)
    spec = parse_spec(
        HEADER
        + f"exclude: [audit_log]\nsubset:\n  jailer: {{model: {jm}, datamodel: {datamodel}}}\n"
    )
    capabilities = Capabilities(formats=(ImageFormat.LOGICAL,), expressions=True, nested=True)
    live = shop()
    planned = plan(spec, live, CatalogLock.of(live), capabilities, PlanOptions())
    assert "Jailer subset: ignored the layout block, which does not choose rows" in planned.warnings
    broken = parse_spec(
        HEADER + f"subset:\n  jailer: {{model: {tmp_path / 'nope.jm'}, datamodel: {datamodel}}}\n"
    )
    with pytest.raises(PlanError) as caught:
        plan(broken, live, CatalogLock.of(live), capabilities, PlanOptions())
    assert caught.value.problems[0].startswith("the Jailer subset: there is no Jailer")


def test_jailer_subsets_stand_alone(tmp_path: Path) -> None:
    with pytest.raises(SpecError) as caught:
        parse_spec(
            HEADER
            + "subset:\n  seeds: {users: 'id = 1'}\n"
            + f"  jailer: {{model: {tmp_path}/m.jm, datamodel: {tmp_path}}}\n"
        )
    assert "cannot also have seeds" in str(caught.value.details)
