# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Vault announce adapter for Datadouble.

Spec settings::

    announce:
      - vault:
          address: https://vault.internal.example:8200
          mount: secret              # a KV version 2 mount
          path: datadouble/clones    # entries go under <path>/<copy id>

Each copy gets one KV v2 secret with its labels, location and credentials.
Releasing a copy deletes the secret and its history. The token comes from
``VAULT_TOKEN`` (and ``VAULT_NAMESPACE`` is honoured); it never appears in the
spec. Works with HashiCorp Vault and OpenBao.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.announce import Announced, AnnounceError, Announcer
from datadouble.settings import parse_settings

_TIMEOUT_SECONDS = 30


class VaultSettings(BaseModel):
    """Settings for the ``vault`` announce adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    address: str = Field(pattern=r"^https?://")
    mount: str = "secret"
    path: str = "datadouble"


class VaultAnnouncer:
    """One KV v2 secret per copy."""

    def __init__(self, settings: VaultSettings, environ: Mapping[str, str]) -> None:
        """Talk to ``settings.address`` with the token in ``environ``."""
        self.settings = settings
        token = environ.get("VAULT_TOKEN")
        if not token:
            message = "VAULT_TOKEN is not set"
            raise AnnounceError(message)
        self.headers = {"X-Vault-Token": token, "Content-Type": "application/json"}
        if namespace := environ.get("VAULT_NAMESPACE"):
            self.headers["X-Vault-Namespace"] = namespace

    def _url(self, kind: str, copy_id: str) -> str:
        path = "/".join(quote(part, safe="") for part in (*self.settings.path.split("/"), copy_id))
        mount = quote(self.settings.mount, safe="")
        return f"{self.settings.address.rstrip('/')}/v1/{mount}/{kind}/{path}"

    def _request(self, method: str, url: str, body: bytes | None, action: str) -> None:
        request = urllib.request.Request(url, data=body, method=method, headers=self.headers)  # noqa: S310 - the address must be http(s), checked in settings
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS):  # noqa: S310 - as above
                pass
        except urllib.error.HTTPError as error:
            message = f"{action} failed: Vault answered {error.code}"
            raise AnnounceError(message) from None
        except urllib.error.URLError as error:
            message = f"{action} failed: {error.reason}"
            raise AnnounceError(message) from None

    def announce(self, copies: tuple[Announced, ...]) -> tuple[str, ...]:
        """Write one secret per copy."""
        written: list[str] = []
        for copy in copies:
            data: dict[str, JsonValue] = {
                "copy_id": copy.copy_id,
                "kind": copy.kind,
                **{f"label_{key.replace('-', '_')}": value for key, value in copy.labels.items()},
                **copy.location,
                **copy.secrets,
            }
            url = self._url("data", copy.copy_id)
            self._request(
                "POST", url, json.dumps({"data": data}).encode(), f"writing {copy.copy_id}"
            )
            written.append(f"vault:{self.settings.mount}/{self.settings.path}/{copy.copy_id}")
        return tuple(written)

    def withdraw(self, copy_ids: tuple[str, ...], /) -> tuple[str, ...]:
        """Delete each copy's secret and its versions."""
        removed: list[str] = []
        for copy_id in copy_ids:
            self._request("DELETE", self._url("metadata", copy_id), None, f"deleting {copy_id}")
            removed.append(f"vault:{self.settings.mount}/{self.settings.path}/{copy_id}")
        return tuple(removed)


def vault_announcer(settings: Mapping[str, JsonValue]) -> Announcer:
    """The entry point named in ``datadouble.announcers``."""
    return VaultAnnouncer(parse_settings(VaultSettings, settings, "vault announce"), os.environ)
