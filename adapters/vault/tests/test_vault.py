# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Vault announce adapter: against a stand-in server, and a real OpenBao."""

import datetime as dt
import http.server
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Generator
from typing import ClassVar

import pytest

from datadouble.announce import Announced, AnnounceError
from datadouble.registry import installed
from datadouble.settings import SettingsError
from datadouble_vault import VaultAnnouncer, VaultSettings, vault_announcer

COPY = Announced(
    copy_id="abc123",
    kind="instance",
    labels={
        "spec": "shop",
        "engine-version": "18",
        "built-at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC).isoformat(),
    },
    location={"host": "db", "port": 5432},
    secrets={"password": "s3cret"},
)


class _Recorder(http.server.BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []
    status = 204

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).calls.append((self.command, self.path, dict(self.headers), body))
        self.send_response(type(self).status)
        self.end_headers()

    do_POST = _record  # noqa: N815 - http.server's naming
    do_DELETE = _record  # noqa: N815 - http.server's naming

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's name
        return


@pytest.fixture
def server() -> Generator[str]:
    _Recorder.calls = []
    _Recorder.status = 204
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_announce_and_withdraw(server: str) -> None:
    announcer = VaultAnnouncer(
        VaultSettings(address=server, mount="kv", path="clones/shop"),
        {"VAULT_TOKEN": "t", "VAULT_NAMESPACE": "team"},
    )
    assert announcer.announce((COPY,)) == ("vault:kv/clones/shop/abc123",)
    assert announcer.withdraw(("abc123",)) == ("vault:kv/clones/shop/abc123",)
    (post, delete) = _Recorder.calls
    assert post[:2] == ("POST", "/v1/kv/data/clones/shop/abc123")
    assert post[2]["X-Vault-Token"] == "t"
    assert post[2]["X-Vault-Namespace"] == "team"
    assert json.loads(post[3])["data"] == {
        "copy_id": "abc123",
        "kind": "instance",
        "label_spec": "shop",
        "label_engine_version": "18",
        "label_built_at": "2026-01-01T00:00:00+00:00",
        "host": "db",
        "port": 5432,
        "password": "s3cret",
    }
    assert delete[:2] == ("DELETE", "/v1/kv/metadata/clones/shop/abc123")


def test_failures(server: str) -> None:
    _Recorder.status = 403
    announcer = VaultAnnouncer(VaultSettings(address=server), {"VAULT_TOKEN": "t"})
    with pytest.raises(AnnounceError, match="writing abc123 failed: Vault answered 403"):
        announcer.announce((COPY,))
    unreachable = VaultAnnouncer(VaultSettings(address="http://127.0.0.1:9"), {"VAULT_TOKEN": "t"})
    with pytest.raises(AnnounceError, match="deleting abc123 failed"):
        unreachable.withdraw(("abc123",))
    with pytest.raises(AnnounceError, match="VAULT_TOKEN is not set"):
        VaultAnnouncer(VaultSettings(address=server), {})
    with pytest.raises(SettingsError, match="address"):
        vault_announcer({"address": "ftp://x"})


def test_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_TOKEN", "t")
    assert "vault" in installed().announcers
    assert isinstance(vault_announcer({"address": "http://x"}), VaultAnnouncer)


@pytest.mark.integration
def test_against_openbao() -> None:
    address = os.environ.get("DATADOUBLE_TEST_VAULT_ADDR")
    if not address:
        pytest.skip("no Vault; run in the Compose default group")
    token = os.environ["VAULT_TOKEN"]
    copy = COPY.model_copy(update={"copy_id": uuid.uuid4().hex[:12]})
    announcer = VaultAnnouncer(VaultSettings(address=address), os.environ)
    announcer.announce((copy,))
    request = urllib.request.Request(  # noqa: S310 - the test server's address
        f"{address}/v1/secret/data/datadouble/{copy.copy_id}", headers={"X-Vault-Token": token}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - as above
        stored = json.loads(response.read())["data"]["data"]
    assert stored["password"] == COPY.secrets["password"]
    announcer.withdraw((copy.copy_id,))
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)  # noqa: S310 - as above
    assert caught.value.code == 404
