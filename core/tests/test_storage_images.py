# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import io
import tarfile
from pathlib import Path

import pytest

from datadouble.images import (
    ImageError,
    digest,
    generations,
    image_lines,
    index_key,
    new_generation,
    pack,
    read_index,
    unpack,
    version_line,
    write_index,
)
from datadouble.names import Generation
from datadouble.settings import SettingsError
from datadouble.storage import (
    FilesystemStore,
    StoreError,
    check_key,
    filesystem_store,
)
from fakes import Unlisted


def test_keys() -> None:
    assert check_key("a/b-1/c.json") == "a/b-1/c.json"
    for bad in ("", "/abs", "a//b", "a/../b", "a/./b", "a b", "a/"):
        with pytest.raises(StoreError, match="not a valid store key"):
            check_key(bad)


def test_filesystem_store(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path / "store")
    assert store.list("") == []
    assert store.get_bytes("x/y") is None
    assert not store.get_file("x/y", tmp_path / "out")
    store.put_bytes("x/y", b"one")
    source = tmp_path / "source"
    source.write_bytes(b"two")
    store.put_file("x/z", source)
    assert store.get_bytes("x/y") == b"one"
    assert store.get_file("x/z", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"two"
    assert store.list("x/") == ["x/y", "x/z"]
    assert store.list("y") == []
    assert store.describe() == f"filesystem:{tmp_path / 'store'}"


def test_failed_writes_leave_nothing(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.put_file("a/b", tmp_path / "missing")
    assert [p.name for p in (tmp_path / "a").iterdir()] == []
    assert store.list("") == []


def test_filesystem_settings(tmp_path: Path) -> None:
    assert isinstance(filesystem_store({"path": str(tmp_path)}), FilesystemStore)
    with pytest.raises(SettingsError, match="filesystem store settings are invalid: path"):
        filesystem_store({"path": 3})


def test_pack_and_unpack(tmp_path: Path) -> None:
    source = tmp_path / "files"
    (source / "deep").mkdir(parents=True)
    (source / "deep" / "data").write_bytes(b"rows")
    archive = tmp_path / "image.tar.gz"
    found = pack(source, b'{"m": 1}', archive)
    assert found == digest(archive)
    assert found.size == archive.stat().st_size
    image, manifest = unpack(archive, tmp_path / "out")
    assert manifest == b'{"m": 1}'
    assert (image / "deep" / "data").read_bytes() == b"rows"
    assert not (tmp_path / "image.tar.gz.manifest.json").exists()


def test_unpack_rejects_other_archives(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not an archive")
    with pytest.raises(ImageError, match=r"cannot unpack junk\.tar\.gz"):
        unpack(junk, tmp_path / "a")
    other = tmp_path / "other.tar.gz"
    with tarfile.open(other, "w:gz") as archive:
        info = tarfile.TarInfo("readme")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(ImageError, match="is not a Datadouble image"):
        unpack(other, tmp_path / "b")
    escape = tmp_path / "escape.tar.gz"
    with tarfile.open(escape, "w:gz") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(ImageError, match="cannot unpack"):
        unpack(escape, tmp_path / "c")
    assert not (tmp_path / "outside").exists()


def test_generations_sort_by_time() -> None:
    early, late = new_generation(1_000), new_generation(2_000)
    assert early < late
    assert len(new_generation()) == 16


def test_a_store_it_cannot_write(tmp_path: Path) -> None:
    (tmp_path / "store").write_text("not a directory", encoding="utf-8")
    store = FilesystemStore(tmp_path / "store")
    with pytest.raises(
        StoreError, match=r"cannot write a/b to filesystem:.*store: Not a directory"
    ):
        store.put_bytes("a/b", b"data")


@pytest.mark.parametrize(
    ("version", "parts", "line"),
    [("8.4.13", 2, "8.4"), ("8.4.13", 1, "8"), ("17.2", 1, "17"), ("18", 2, "18")],
)
def test_version_lines(version: str, parts: int, line: str) -> None:
    assert version_line(version, parts) == line


def test_stores_that_cannot_list_read_the_index(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path)
    for key in (
        "shop/all/fake-9/0002.summary.json",
        "shop/all/fake-18/0001.summary.json",
        "shop/all/fake-18/0003.summary.json",
        "shop/all/fake-18/0003.physical.tar.gz",
        "shop/all/fake-18/channels/latest.json",
    ):
        store.put_bytes(key, b"{}")
    unlisted = Unlisted(tmp_path)
    assert image_lines(unlisted, "shop", "all") == []
    index = write_index(store, "shop", "all")
    assert index.lines == {"fake-9": ("0002",), "fake-18": ("0001", "0003")}
    assert read_index(unlisted, "shop", "all") == index
    assert image_lines(unlisted, "shop", "all") == image_lines(store, "shop", "all")
    assert image_lines(unlisted, "shop", "all") == ["shop/all/fake-9", "shop/all/fake-18"]
    assert generations(unlisted, "shop/all/fake-18") == [Generation("0001"), Generation("0003")]
    assert generations(unlisted, "shop/all/fake-17") == []
    assert store.list("shop/all/index") == [index_key("shop", "all")]
    store.put_bytes(index_key("shop", "all"), b'{"lines": {"fake-18": 3}}')
    with pytest.raises(ImageError, match=r"^shop/all/index\.json in filesystem:.* is not a valid"):
        image_lines(unlisted, "shop", "all")
