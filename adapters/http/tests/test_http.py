# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The HTTP store, against an in-process transport standing in for a web server."""

import base64
import gzip
import secrets
import ssl
from collections.abc import Callable
from pathlib import Path

import certifi
import httpx
import pytest

from datadouble.images import ImageIndex, image_lines, index_key
from datadouble.names import Generation
from datadouble.registry import installed
from datadouble.secrets import SecretError
from datadouble.settings import SettingsError
from datadouble.spec import EnvSecret
from datadouble.storage import ListingUnsupportedError, StoreError
from datadouble_http import BearerAuth, HttpSettings, HttpStore, client, http_store

URL = "https://files.example.com/exports/"
type Handler = Callable[[httpx.Request], httpx.Response]


def store(files: dict[str, bytes], seen: list[httpx.Request] | None = None) -> HttpStore:
    """A store whose server answers from ``files``, keyed by URL path."""

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        data = files.get(request.url.path)
        # A stream, as a real transport gives, so downloads read the raw body.
        return httpx.Response(404) if data is None else httpx.Response(200, stream=_bytes(data))

    return served(answer)


def _bytes(data: bytes) -> httpx.ByteStream:
    return httpx.ByteStream(data)


def served(handler: Handler) -> HttpStore:
    settings = HttpSettings(url=URL)
    return HttpStore(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def test_reads_keys_under_the_url(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    files = {"/exports/shop/all/fake-18/0001.summary.json": b"{}", "/exports/big": b"x" * 3_000_000}
    http = store(files, seen)
    assert http.describe() == URL
    assert http.get_bytes("shop/all/fake-18/0001.summary.json") == b"{}"
    assert http.get_bytes("shop/all/missing") is None
    assert http.get_file("big", tmp_path / "big")
    assert (tmp_path / "big").read_bytes() == b"x" * 3_000_000
    assert not http.get_file("missing", tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["big"]
    assert [str(r.url) for r in seen][:2] == [
        f"{URL}shop/all/fake-18/0001.summary.json",
        f"{URL}shop/all/missing",
    ]
    with pytest.raises(StoreError, match="not a valid store key"):
        http.get_bytes("../etc/passwd")


def test_is_read_only(tmp_path: Path) -> None:
    http = store({})
    with pytest.raises(StoreError, match=f"{URL} is a read-only http store: cannot write a/b"):
        http.put_bytes("a/b", b"")
    with pytest.raises(StoreError, match="read-only http store: cannot write a/c"):
        http.put_file("a/c", tmp_path / "c")
    with pytest.raises(StoreError, match="read-only http store: cannot delete a/d"):
        http.delete("a/d")
    with pytest.raises(ListingUnsupportedError, match="cannot list keys"):
        http.list("")


def test_lists_through_the_index() -> None:
    index = ImageIndex(lines={"fake-18": (Generation("0002"),), "fake-9": (Generation("0001"),)})
    http = store({f"/exports/{index_key('shop', 'all')}": index.model_dump_json().encode()})
    assert image_lines(http, "shop", "all") == ["shop/all/fake-9", "shop/all/fake-18"]
    assert image_lines(http, "shop", "other") == []


def test_errors_name_the_path_and_status_not_the_body(tmp_path: Path) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"secret page body")

    http = served(refuse)
    with pytest.raises(StoreError, match=r"^reading /exports/a failed: HTTP 403$"):
        http.get_bytes("a")
    with pytest.raises(StoreError, match=r"^downloading /exports/a failed: HTTP 403$"):
        http.get_file("a", tmp_path / "a")
    assert list(tmp_path.iterdir()) == []

    def unreachable(request: httpx.Request) -> httpx.Response:
        message = "no route"
        raise httpx.ConnectError(message, request=request)

    http = served(unreachable)
    with pytest.raises(StoreError, match=r"^reading /exports/a failed: ConnectError$"):
        http.get_bytes("a")
    with pytest.raises(StoreError, match=r"^downloading /exports/a failed: ConnectError$"):
        http.get_file("a", tmp_path / "a")
    assert list(tmp_path.iterdir()) == []


def test_a_failure_mid_download_leaves_nothing(tmp_path: Path) -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> "Broken":
            return self

        def __next__(self) -> bytes:
            message = "connection reset"
            raise httpx.ReadError(message)

    http = served(lambda _request: httpx.Response(200, stream=Broken()))
    with pytest.raises(StoreError, match="downloading /exports/a failed: ReadError"):
        http.get_file("a", tmp_path / "a")
    assert list(tmp_path.iterdir()) == []


def test_settings() -> None:
    assert "http" in installed().stores
    assert http_store({"url": "https://files.example.com/x"}).describe() == (
        "https://files.example.com/x/"
    )
    parsed = HttpSettings.model_validate_json('{"url": "http://h/", "timeout_seconds": 5}')
    assert parsed.timeout_seconds == 5
    for url, problem in [
        ("ftp://files.example.com/", "must be an http"),
        ("https:///path", "must be an http"),
        ("https://user:pw@files.example.com/", "must not hold credentials"),
        ("https://files.example.com/?a=1", "must not have a query"),
    ]:
        with pytest.raises(
            SettingsError, match=f"http store settings are invalid: url: .*{problem}"
        ):
            http_store({"url": url})
    with pytest.raises(SettingsError, match="timeout_seconds"):
        http_store({"url": URL, "timeout_seconds": 0})
    # Credentials only travel over TLS.
    with pytest.raises(SettingsError, match="`auth` needs an https:// URL"):
        http_store({"url": "http://files.example.com/", "auth": {"bearer": {"env": "TOKEN"}}})
    with pytest.raises(SettingsError, match="auth"):
        http_store({"url": URL, "auth": {"digest": {"env": "X"}}})


def test_credentials_come_from_secret_references(monkeypatch: pytest.MonkeyPatch) -> None:
    user, password, token = (secrets.token_hex(8) for _ in range(3))
    monkeypatch.setenv("EXPORT_USER", user)
    monkeypatch.setenv("EXPORT_PASSWORD", password)
    monkeypatch.setenv("EXPORT_TOKEN", token)
    basic = {"basic": {"username": {"env": "EXPORT_USER"}, "password": {"env": "EXPORT_PASSWORD"}}}
    for auth, expected in [
        (basic, "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()),
        ({"bearer": {"env": "EXPORT_TOKEN"}}, f"Bearer {token}"),
        (None, None),
    ]:
        built = client(HttpSettings.model_validate({"url": URL, "auth": auth}))
        request = built.build_request("GET", URL)
        if built.auth is not None:
            request = next(built.auth.sync_auth_flow(request))
        assert request.headers.get("Authorization") == expected
    monkeypatch.delenv("EXPORT_TOKEN")
    with pytest.raises(SecretError, match="EXPORT_TOKEN is not set"):
        client(HttpSettings(url=URL, auth=BearerAuth(bearer=EnvSecret(env="EXPORT_TOKEN"))))


def test_ca_bundles(tmp_path: Path) -> None:
    assert http_store({"url": URL, "ca_bundle": certifi.where()}).describe() == URL
    with pytest.raises(StoreError, match=r"cannot use the CA bundle .*missing\.pem"):
        http_store({"url": URL, "ca_bundle": str(tmp_path / "missing.pem")})
    (tmp_path / "bad.pem").write_text("not a certificate\n", encoding="utf-8")
    with pytest.raises(StoreError, match=r"cannot use the CA bundle .*bad\.pem"):
        http_store({"url": URL, "ca_bundle": str(tmp_path / "bad.pem")})


def test_tls_verification_failures_are_named(tmp_path: Path) -> None:
    def untrusted(request: httpx.Request) -> httpx.Response:
        error = httpx.ConnectError("handshake failed", request=request)
        error.__cause__ = ssl.SSLCertVerificationError(1, "self-signed certificate in chain")
        raise error

    http = served(untrusted)
    expected = "failed: ConnectError \\(certificate verification failed; check ca_bundle\\)$"
    with pytest.raises(StoreError, match=f"^reading /exports/a {expected}") as caught:
        http.get_bytes("a")
    assert "self-signed" not in str(caught.value)
    with pytest.raises(StoreError, match=f"^downloading /exports/a {expected}"):
        http.get_file("a", tmp_path / "a")


def test_downloads_keep_the_bytes_as_served(tmp_path: Path) -> None:
    archive = gzip.compress(b"rows")
    asked: list[str | None] = []

    def compressing(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("Accept-Encoding"))
        headers = {"Content-Encoding": "gzip"}
        return httpx.Response(200, stream=_bytes(archive), headers=headers)

    http = served(compressing)
    assert http.get_file("a.tar.gz", tmp_path / "a.tar.gz")
    assert (tmp_path / "a.tar.gz").read_bytes() == archive
    assert asked == ["identity"]
