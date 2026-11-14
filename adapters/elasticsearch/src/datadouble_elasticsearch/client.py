# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Requests to Elasticsearch's REST API, through elastic-transport.

The adapter talks to the REST API directly, with Elastic's own transport for
connections and retries, rather than through the official client: each major
version of that client talks only to servers of its own major version, and
the adapter reads sources of any version from 7.17 on.

Replies are parsed JSON, read with the helpers here. Errors name the
server's error type and HTTP status, never its reason: a reason can quote
the document that caused it.
"""

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final
from urllib.parse import quote, unquote, urlencode, urlsplit

from elastic_transport import NodeConfig, Transport, TransportError
from pydantic import JsonValue, TypeAdapter, ValidationError

from datadouble.engine import EngineError

type Document = dict[str, object]
type Params = Mapping[str, str | int | bool]

_SCHEMES: Final = {"http": 80, "https": 443}
_DOCUMENT: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
_LIST: TypeAdapter[list[object]] = TypeAdapter(list[object])
_TIMEOUT = 120.0
"""Seconds a request may take, unless it waits for a long operation."""
LONG = 3600.0
"""Seconds for requests that wait for a snapshot, restore, update or bulk load to finish."""


def as_document(value: object) -> Document | None:
    """``value`` as a JSON object, if it is one."""
    if not isinstance(value, Mapping):
        return None
    try:
        return _DOCUMENT.validate_python(value)
    except ValidationError:
        return None


def as_list(value: object) -> list[object] | None:
    """``value`` as a list, if it is one."""
    return _LIST.validate_python(value) if isinstance(value, list) else None


def document(value: object, what: str) -> Document:
    """``value`` as a JSON object, or an error naming ``what``."""
    found = as_document(value)
    if found is None:
        message = f"expected a JSON object for {what}"
        raise EngineError(message)
    return found


def items(value: object, what: str) -> list[object]:
    """``value`` as a list, or an error naming ``what``."""
    found = as_list(value)
    if found is None:
        message = f"expected a JSON array for {what}"
        raise EngineError(message)
    return found


def text(value: object) -> str:
    """A scalar as text, as Elasticsearch compares it: ``true``, ``42``, ``alice``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _error_type(body: object) -> str:
    """The type of a reply's error, or of the cause of its first failure."""
    reply = as_document(body) or {}
    failures = as_list(reply.get("failures")) or [{}]
    error = as_document(reply.get("error")) or as_document(
        (as_document(failures[0]) or {}).get("cause")
    )
    kind = error.get("type") if error else None
    return kind if isinstance(kind, str) else "error"


def path(*parts: str) -> str:
    """A request path, each part quoted: ``path("films", "_doc", "1")``."""
    return "/" + "/".join(quote(part, safe=",*") for part in parts)


class Client:
    """A connection to one cluster."""

    def __init__(self, transport: Transport, headers: Mapping[str, str]) -> None:
        """Send requests through ``transport`` with ``headers``."""
        self._transport = transport
        self._headers = dict(headers)

    @classmethod
    def from_url(cls, url: str, api_key: str | None = None) -> "Client":
        """A client for the cluster at an ``http://`` or ``https://`` URL.

        Credentials in the URL are sent as basic authentication; an API key,
        if given, is sent instead.
        """
        parts = urlsplit(url)
        if parts.scheme not in _SCHEMES or not parts.hostname:
            message = "an Elasticsearch URL is http[s]://[user:password@]host[:port]"
            raise EngineError(message)
        headers: dict[str, str] = {}
        if api_key:
            headers["authorization"] = f"ApiKey {api_key}"
        elif parts.username:
            user, password = unquote(parts.username), unquote(parts.password or "")
            headers["authorization"] = _basic(user, password)
        node = NodeConfig(
            parts.scheme,
            parts.hostname,
            parts.port or _SCHEMES[parts.scheme],
            path_prefix=parts.path.rstrip("/"),
        )
        return cls(_transport(node), headers)

    @classmethod
    def at(cls, host: str, port: int, password: str) -> "Client":
        """A client for a working server, as its ``elastic`` user."""
        return cls(
            _transport(NodeConfig("http", host, port)),
            {"authorization": _basic("elastic", password)},
        )

    def request(
        self,
        method: str,
        target: str,
        action: str,
        *,
        body: Mapping[str, JsonValue] | None = None,
        params: Params | None = None,
    ) -> Document:
        """Send one request and return its JSON reply.

        A request with ``wait_for_completion`` may wait for an hour.
        """
        query = "?" + urlencode({k: text(v) for k, v in params.items()}) if params else ""
        waits = bool(params and params.get("wait_for_completion"))
        return self._send(
            _Call(method, target + query, action, body, timeout=LONG if waits else _TIMEOUT)
        )

    def bulk(self, lines: Sequence[Mapping[str, JsonValue]], action: str) -> None:
        """Send a ``_bulk`` request; fail if any of its operations failed."""
        if not lines:
            return
        reply = self._send(
            _Call("POST", "/_bulk", action, list(lines), LONG, "application/x-ndjson")
        )
        if reply.get("errors") is not True:
            return
        for item in items(reply.get("items"), "bulk results"):
            for result in document(item, "a bulk result").values():
                outcome = document(result, "a bulk result")
                if "error" in outcome:
                    status = outcome.get("status")
                    message = f"{action} failed: {_error_type(outcome)} (HTTP {status})"
                    raise EngineError(message)

    def _send(self, call: "_Call") -> Document:
        headers = {**self._headers, "accept": "application/json", "content-type": call.content}
        try:
            response = self._transport.perform_request(
                call.method,
                call.target,
                body=call.body,
                headers=headers,
                request_timeout=call.timeout,
            )
        except TransportError as error:
            message = f"{call.action} failed: {type(error).__name__}"
            raise EngineError(message) from None
        status = response.meta.status
        if not HTTPStatus(status).is_success:
            message = f"{call.action} failed: {_error_type(response.body)} (HTTP {status})"
            raise EngineError(message)
        return as_document(response.body) or {}

    def close(self) -> None:
        """Close the connections."""
        self._transport.close()


@dataclass(frozen=True, slots=True)
class _Call:
    method: str
    target: str
    action: str
    body: object
    timeout: float = _TIMEOUT
    content: str = "application/json"


def _transport(node: NodeConfig) -> Transport:
    # Overloaded nodes answer 429 or 503: retry those after a growing pause.
    return Transport([node], retry_backoff_base=1, retry_backoff_cap=30)


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
