# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import hashlib
import io
import urllib.request
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


def test_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = SCRIPT.encode()
    monkeypatch.setattr(sakila_bulk, "download", lambda: data)
    monkeypatch.setattr(sakila_bulk, "OUTPUT", tmp_path / "out")
    monkeypatch.setattr(sakila_bulk, "SOURCE_SHA256", hashlib.sha256(data).hexdigest())
    assert sakila_bulk.main() == 0
    assert capsys.readouterr().out == "actor: 2\nfilm: 1\n"
    actor = (tmp_path / "out" / "actor.tsv").read_text("utf-8").splitlines()
    assert actor == [
        "actor_id\tfirst_name\tnote",
        "1\tO'Brien\t\\N",
        "2\tTab\\there\tback\\\\\\\\slash\\nnewline",
    ]
    assert (tmp_path / "out" / "tables.txt").read_text("utf-8") == "actor\t2\nfilm\t1\n"
    monkeypatch.setattr(sakila_bulk, "SOURCE_SHA256", "0" * 64)
    assert sakila_bulk.main() == 1
    assert "not 000" in capsys.readouterr().out


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


def test_download(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    class Response(io.BytesIO):
        pass

    def urlopen(url: str, timeout: int) -> Response:
        opened.append(url)
        assert timeout == 60
        return Response(b"script")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert sakila_bulk.download() == b"script"
    assert opened[0].startswith("https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1")
