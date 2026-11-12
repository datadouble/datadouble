# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

import sakila_bulk

SCRIPT = """
-- table actor
Insert into actor
 (`actor_id`,`first_name`,`note`)
Values
('1','O''Brien',NULL)
;
Insert into actor
 (`actor_id`,`first_name`,`note`)
Values
('2','Tab\there','back\\\\slash
newline')
;
Insert into film (`film_id`) Values ('7');
"""


def test_convert(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "insert.sql"
    script.write_text(SCRIPT, "utf-8")
    assert sakila_bulk.main([str(script), str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out == "actor: 2\nfilm: 1\n"
    actor = (tmp_path / "out" / "actor.tsv").read_text("utf-8").splitlines()
    assert actor == [
        "actor_id\tfirst_name\tnote",
        "1\tO'Brien\t\\N",
        "2\tTab\\there\tback\\\\\\\\slash\\nnewline",
    ]
    assert (tmp_path / "out" / "tables.txt").read_text("utf-8") == "actor\t2\nfilm\t1\n"


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("Insert into t (`a`) Values (5);", "unexpected value"),
        ("Insert into t (`a`) Values ('1','2');", "2 values for 1 columns"),
        (
            "Insert into t (`a`) Values ('1');\nInsert into t (`b`) Values ('1');",
            "column list changes",
        ),
    ],
)
def test_malformed_scripts(tmp_path: Path, script: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        sakila_bulk.convert(script, tmp_path)


def test_trailing_separators() -> None:
    assert list(sakila_bulk.values("'a', ")) == ["a"]
