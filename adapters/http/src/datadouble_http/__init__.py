# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The read-only HTTP(S) storage adapter for Datadouble.

Spec settings::

    fetch:
      from:
        http:
          url: https://files.example.com/datadouble/
          auth:                                       # optional
            basic:
              username:
                env: EXPORT_USER
              password:
                env: EXPORT_PASSWORD
            # or bearer:
            #   env: EXPORT_TOKEN
          ca_bundle: /etc/ssl/internal-ca.pem          # optional
          timeout_seconds: 60                          # optional

Keys map to URLs under ``url``. A web server has no standard way to list
files, so ``list`` is unsupported and ``fetch`` reads the index ``promote``
writes under each ``<spec>/<group>/``. The store is read-only: ``promote``
writes with the filesystem or S3 store into the directory the server serves.
Credentials are secret references; errors name the URL path and the HTTP
status, never response bodies or credentials.
"""

import os
import ssl
import tempfile
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Self, assert_never
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import SecretRef
from datadouble.storage import ListingUnsupportedError, Store, StoreError, check_key

_CHUNK = 1 << 20
_IDENTITY = {"Accept-Encoding": "identity"}


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class BasicCredentials(_Model):
    """A user name and password, each a secret reference."""

    username: SecretRef
    password: SecretRef


class BasicAuth(_Model):
    """HTTP basic authentication."""

    basic: BasicCredentials


class BearerAuth(_Model):
    """A bearer token, sent as ``Authorization: Bearer ...``."""

    bearer: SecretRef


class HttpSettings(_Model):
    """The settings of one ``http`` store."""

    url: str
    auth: BasicAuth | BearerAuth | None = None
    ca_bundle: Path | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            message = "must be an http:// or https:// URL"
            raise ValueError(message)
        if parts.username is not None or parts.password is not None:
            message = "must not hold credentials; use `auth`"
            raise ValueError(message)
        if parts.query or parts.fragment:
            message = "must not have a query or fragment"
            raise ValueError(message)
        return value if value.endswith("/") else f"{value}/"

    @model_validator(mode="after")
    def _credentials_over_tls(self) -> Self:
        # A password or token sent over plain HTTP crosses the network readable.
        if self.auth is not None and urlsplit(self.url).scheme != "https":
            message = "`auth` needs an https:// URL; credentials are never sent in clear text"
            raise ValueError(message)
        return self


class HttpStore:
    """Images served by a web server under one URL; reads only."""

    def __init__(self, settings: HttpSettings, client: httpx.Client) -> None:
        """Use ``client`` for every request."""
        self.settings = settings
        self.client = client

    def _url(self, key: str) -> str:
        return self.settings.url + check_key(key)

    def describe(self) -> str:
        """The base URL, which never holds credentials."""
        return self.settings.url

    def _read_only(self, action: str, key: str) -> str:
        return f"{self.describe()} is a read-only http store: cannot {action} {key}"

    def put_file(self, key: str, source: Path) -> None:
        """Refuse: the store is read-only."""
        del source
        message = self._read_only("write", key)
        raise StoreError(message)

    def put_bytes(self, key: str, data: bytes) -> None:
        """Refuse: the store is read-only."""
        del data
        message = self._read_only("write", key)
        raise StoreError(message)

    def delete(self, key: str) -> None:
        """Refuse: the store is read-only."""
        message = self._read_only("delete", key)
        raise StoreError(message)

    def list(self, prefix: str) -> list[str]:
        """Refuse: a web server has no standard listing, so readers use the index."""
        message = f"{self.describe()} cannot list keys under {prefix or '/'}; read its index"
        raise ListingUnsupportedError(message)

    def get_bytes(self, key: str) -> bytes | None:
        """A key's bytes, or None on a 404."""
        url = self._url(key)
        try:
            response = self.client.get(url)
        except httpx.HTTPError as error:
            message = _failure("reading", url, error)
            raise StoreError(message) from None
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        _check("reading", url, response)
        return response.content

    def get_file(self, key: str, destination: Path) -> bool:
        """Stream a key to a temporary file beside ``destination``, then rename it."""
        url = self._url(key)
        handle, name = tempfile.mkstemp(dir=destination.parent, prefix=".partial-")
        os.close(handle)
        partial = Path(name)
        try:
            found = self._download(url, partial)
            if found:
                partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        return found

    def _download(self, url: str, partial: Path) -> bool:
        # The archive's bytes exactly as stored: no Content-Encoding is asked for, and
        # none a server applies anyway is undone, so the hash check sees what is served.
        try:
            with self.client.stream("GET", url, headers=_IDENTITY) as response:
                if response.status_code == HTTPStatus.NOT_FOUND:
                    return False
                _check("downloading", url, response)
                with partial.open("wb") as out:
                    for chunk in response.iter_raw(_CHUNK):
                        out.write(chunk)
        except httpx.HTTPError as error:
            message = _failure("downloading", url, error)
            raise StoreError(message) from None
        return True


def _path(url: str) -> str:
    return urlsplit(url).path


def _check(action: str, url: str, response: httpx.Response) -> None:
    """Fail on any status but success, naming the URL path and the status, not the body."""
    if not response.is_success:
        message = f"{action} {_path(url)} failed: HTTP {response.status_code}"
        raise StoreError(message)


def _failure(action: str, url: str, error: httpx.HTTPError) -> str:
    """A message naming the URL path and the kind of failure, never the error's text."""
    return f"{action} {_path(url)} failed: {_kind(error)}"


def _kind(error: BaseException) -> str:
    """The error's type, or a certificate verification failure anywhere in its causes."""
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return f"{type(error).__name__} (certificate verification failed; check ca_bundle)"
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return type(error).__name__


def _verify(settings: HttpSettings) -> ssl.SSLContext | bool:
    if settings.ca_bundle is None:
        return True
    try:
        return ssl.create_default_context(cafile=settings.ca_bundle)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        message = f"cannot use the CA bundle {settings.ca_bundle}: {reason}"
        raise StoreError(message) from None


def client(settings: HttpSettings) -> httpx.Client:
    """An HTTP client with the store's credentials, trust and timeout."""
    auth: httpx.BasicAuth | None = None
    headers: dict[str, str] = {}
    match settings.auth:
        case None:
            pass
        case BasicAuth(basic=credentials):
            auth = httpx.BasicAuth(
                resolve_secret(credentials.username), resolve_secret(credentials.password)
            )
        case BearerAuth(bearer=token):
            headers["Authorization"] = f"Bearer {resolve_secret(token)}"
        case _:
            assert_never(settings.auth)
    return httpx.Client(
        auth=auth,
        headers=headers,
        verify=_verify(settings),
        timeout=settings.timeout_seconds,
        follow_redirects=True,
    )


def http_store(settings: Mapping[str, JsonValue]) -> Store:
    """The entry point named in ``datadouble.stores``."""
    parsed = parse_settings(HttpSettings, settings, "http store")
    return HttpStore(parsed, client(parsed))
