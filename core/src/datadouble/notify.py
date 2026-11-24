# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Notifications from generated CI jobs (decision 125).

A job ``ci init`` writes names itself in its requests (``ci.job``). When its
command ends with an outcome the spec's ``notify`` section lists, Datadouble
posts a :class:`Notification` to each webhook target itself, after the
command and before the job ends, so webhooks work the same way on every CI
system. Email goes through the CI system's own mechanism, which the CI/CD
adapter writes, or explains in ``ci init``'s instructions.

A notification that fails never changes the job's outcome: the problem is
returned for the caller to log, and the job ends as it would have. The
payload, its ``text`` line included, carries names, codes and outcomes, never
data values or secrets (decisions 18 and 54), and problems never quote the
webhook's URL, which is a secret.
"""

import base64
import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import Message
from typing import IO, override
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from datadouble import __version__
from datadouble.ci import JobKind
from datadouble.outcome import Outcome
from datadouble.secrets import SecretError, resolve_secret
from datadouble.spec import Notify, WebhookFormat

TIMEOUT_SECONDS = 10
"""How long a webhook may take to answer; a job never waits on one for longer."""


class Notification(BaseModel):
    """The JSON body posted to a webhook: what ran and how it ended, never data."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    spec: str
    """The spec's name."""
    job: JobKind
    """The generated job: ``build``, ``promote``, ``fetch``, ``provision`` and so on."""
    outcome: Outcome
    generation: str | None = None
    """The generation a ``build`` made or resumed, when it got that far."""
    error: str | None = None
    """The error code of a failed request, such as ``store_failed``."""
    run_url: str | None = None
    """The CI system's page for the run, where it has one."""

    @property
    def text(self) -> str:
        """One line a person can read, built from the other fields.

        Chat incoming webhooks (Slack, Mattermost, Rocket.Chat) show ``text``
        and ignore the other keys, such as
        ``Datadouble shop: build failed (store_failed) - https://ci.example.com/7``.
        """
        line = f"Datadouble {self.spec}: {self.job} {_SAID[self.outcome]}"
        if self.error is not None:
            line += f" ({self.error})"
        if self.generation is not None:
            line += f", generation {self.generation}"
        if self.run_url is not None:
            line += f" - {self.run_url}"
        return " ".join(line.split())

    def body(self, shape: WebhookFormat = WebhookFormat.JSON) -> bytes:
        """The JSON posted: every field then ``text``, or for ``chat`` only ``text``."""
        if shape is WebhookFormat.CHAT:
            return json.dumps({"text": self.text}).encode()
        return json.dumps({**self.model_dump(mode="json"), "text": self.text}).encode()


_SAID = {
    Outcome.FAILURE: "failed",
    Outcome.WARNING: "passed with warnings",
    Outcome.SUCCESS: "succeeded",
}
"""How ``text`` says each outcome."""


class NotifyError(Exception):
    """A webhook that cannot be posted to."""


type Post = Callable[[str, bytes], None]
"""Posts a JSON body to a URL, raising on any failure."""


class _Post(urllib.request.Request):
    """A JSON POST; the URL's scheme is checked before one is made."""

    def __init__(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(url, data=body, headers=headers, method="POST")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx answer is a failure, like any status but 2xx."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def tls_context() -> ssl.SSLContext:
    """The TLS settings webhooks are posted with: certificates and host names verified."""
    return ssl.create_default_context()


def post(url: str, body: bytes) -> None:
    """POST ``body`` as JSON to ``url``; any status but 2xx raises.

    The environment's proxy settings apply (``HTTPS_PROXY``, ``HTTP_PROXY``,
    ``NO_PROXY``). Credentials in the URL (``https://user:token@host/...``)
    are sent as basic authentication, and only over https.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        message = "its URL must start with https:// or http:// and name a host"
        raise NotifyError(message)
    headers = {"Content-Type": "application/json", "User-Agent": f"datadouble/{__version__}"}
    if parts.username is not None:
        if parts.scheme != "https":
            message = "a URL with credentials must use https://"
            raise NotifyError(message)
        pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    bare = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=tls_context()),
        _NoRedirects(),
    )
    try:
        with opener.open(_Post(bare, body, headers), timeout=TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as error:
        error.close()
        message = f"the server answered with HTTP status {error.code}"
        raise NotifyError(message) from None


def _problem(error: Exception) -> str:
    """Why a post failed, without the URL or anything the server sent."""
    match error:
        case NotifyError() | SecretError():
            return str(error)
        case urllib.error.URLError(reason=BaseException() as reason):
            return f"the request failed ({type(reason).__name__})"
        case _:
            return f"the request failed ({type(error).__name__})"


def send(
    notify: Notify | None,
    notification: Notification,
    *,
    environ: Mapping[str, str] | None = None,
    poster: Post = post,
) -> tuple[str, ...]:
    """Post to every webhook target, if the outcome is one ``notify`` lists.

    Returns a problem per webhook that failed; never raises for one.
    """
    if notify is None or not notify.sends(notification.outcome):
        return ()
    problems: list[str] = []
    for index, webhook in enumerate(notify.webhooks(), start=1):
        try:
            poster(resolve_secret(webhook.url, environ), notification.body(webhook.format))
        except (
            NotifyError,
            SecretError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as error:
            problems.append(f"webhook {index}: {_problem(error)}")
    return tuple(problems)
