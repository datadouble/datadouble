# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Notifications from generated CI jobs: the spec's `notify` section and its webhooks."""

import base64
import json
import secrets
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import pytest

from datadouble import api
from datadouble.ci import CiBridge, JobKind, Report
from datadouble.notify import (
    RUNNING,
    TIMEOUT_SECONDS,
    Notification,
    NotifyError,
    post,
    recall,
    recovered,
    remember,
    send,
    tls_context,
)
from datadouble.outcome import Outcome
from datadouble.spec import EnvSecret, NotifyOn, SpecError, WebhookFormat, parse_spec

NOTIFY = """
notify:
  when: [failure, warning]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com, oncall@example.com]
    - email:
        to: [data-team@example.com]
"""
BASE = "name: shop\nsources: {app: {engine: fake}}\n"
PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "ALL_PROXY", "all_proxy", "REQUEST_METHOD",
)  # fmt: skip


@dataclass
class Received:
    """What a webhook was sent."""

    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """A local webhook that records requests and answers with ``status``."""

    url: str
    status: int = 204
    received: list[Received] = field(default_factory=list[Received])


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Generator[Receiver]:
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    state = Receiver(url="")

    def record(handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length)
        state.received.append(Received(handler.path, dict(handler.headers.items()), body))
        handler.send_response(state.status)
        if HTTPStatus(state.status).is_redirection:
            handler.send_header("Location", "/elsewhere")
        handler.end_headers()

    # http.server calls do_POST; building the class names it without a lint exception.
    handler = type("Handler", (BaseHTTPRequestHandler,), {"do_POST": record})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def notification(outcome: Outcome = Outcome.FAILURE) -> Notification:
    return Notification(
        spec="shop",
        job=JobKind.BUILD,
        outcome=outcome,
        generation="0193a7c21b4e9f30",
        error="store_failed",
        run_url="https://ci.example.com/runs/7",
    )


def test_the_notify_section() -> None:
    spec = parse_spec(BASE + NOTIFY)
    assert spec.notify is not None
    assert spec.notify.when == (NotifyOn.FAILURE, NotifyOn.WARNING)
    assert spec.notify.webhooks()[0].url == EnvSecret(env="ALERTS_WEBHOOK")
    assert spec.notify.emails() == ("data-team@example.com", "oncall@example.com")
    assert spec.notify.sends("warning")
    assert not spec.notify.sends("success")
    dumped = spec.model_dump(mode="json", by_alias=True, exclude_none=True)["notify"]
    assert dumped["when"] == ["failure", "warning"]
    plain = parse_spec(BASE + "notify: {to: [{webhook: {url: {file: /run/hook}}}]}\n")
    assert plain.notify is not None
    assert plain.notify.when == (NotifyOn.FAILURE,)
    assert plain.notify.emails() == ()
    json_spec = parse_spec(
        json.dumps(
            {
                "name": "shop",
                "sources": {"app": {"engine": "fake"}},
                "notify": {"when": ["warning"], "to": [{"email": {"to": ["a@example.com"]}}]},
            }
        )
    )
    assert json_spec.notify is not None
    assert json_spec.notify.when == (NotifyOn.WARNING,)


@pytest.mark.parametrize(
    "section",
    [
        "notify: {to: []}",
        "notify: {on: [failure], to: [{webhook: {url: {env: X}}}]}",
        "notify: {when: [], to: [{webhook: {url: {env: X}}}]}",
        "notify: {when: [success], to: [{webhook: {url: {env: X}}}]}",
        "notify: {to: [{webhook: {url: https://hooks.example.com/x}}]}",
        "notify: {to: [{email: {to: [nobody]}}]}",
        "notify: {to: [{email: {to: []}}]}",
        "notify: {to: [{sms: {to: [x]}}]}",
    ],
)
def test_invalid_notify_sections(section: str) -> None:
    with pytest.raises(SpecError):
        parse_spec(BASE + section + "\n")


def test_posting(receiver: Receiver) -> None:
    post(receiver.url + "/hooks/shop?channel=data", b'{"a": 1}')
    (got,) = receiver.received
    assert got.path == "/hooks/shop?channel=data"
    assert got.headers["Content-Type"] == "application/json"
    assert got.headers["User-Agent"].startswith("datadouble/")
    assert "Authorization" not in got.headers
    assert got.body == b'{"a": 1}'
    post(receiver.url, b"{}")
    assert receiver.received[1].path == "/"
    receiver.status = 500
    with pytest.raises(NotifyError, match="HTTP status 500"):
        post(receiver.url, b"{}")
    receiver.status = 302
    with pytest.raises(NotifyError, match="HTTP status 302"):
        post(receiver.url, b"{}")
    assert [r.path for r in receiver.received[-1:]] == ["/"]
    with pytest.raises(NotifyError, match="must start with https:// or http://"):
        post("file:///etc/passwd", b"{}")
    with pytest.raises(urllib.error.URLError):
        post(receiver.url.replace("http://", "https://"), b"{}")


def test_credentials_need_https(monkeypatch: pytest.MonkeyPatch) -> None:
    token = secrets.token_hex(8)
    with pytest.raises(NotifyError, match="credentials must use https://"):
        post(f"http://hooks:{token}@hooks.example.com/x", b"{}")
    sent: list[urllib.request.Request] = []

    @dataclass
    class Answer:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            del exc

        def read(self) -> bytes:
            return b""

    @dataclass
    class Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> Answer:
            assert timeout == TIMEOUT_SECONDS
            sent.append(request)
            return Answer()

    def build_opener(*handlers: object) -> Opener:
        del handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    post(f"https://hooks:{token}@[::1]:8443/x?y=1", b"{}")
    (request,) = sent
    assert request.full_url == "https://[::1]:8443/x?y=1"
    assert token not in request.full_url
    authorization = request.get_header("Authorization")
    assert authorization is not None
    assert base64.b64decode(authorization.removeprefix("Basic ")) == f"hooks:{token}".encode()


def test_proxies(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", receiver.url)
    monkeypatch.setenv("http_proxy", receiver.url)
    post("http://hooks.example.com/shop?channel=data", b"{}")
    (proxied,) = receiver.received
    assert proxied.path == "http://hooks.example.com/shop?channel=data"
    unused = f"http://127.0.0.1:{closed_port()}"
    monkeypatch.setenv("HTTP_PROXY", unused)
    monkeypatch.setenv("http_proxy", unused)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    post(receiver.url + "/direct", b"{}")
    assert receiver.received[1].path == "/direct"


def test_the_text_line() -> None:
    assert notification().text == (
        "Datadouble shop: build failed (store_failed), generation 0193a7c21b4e9f30"
        " - https://ci.example.com/runs/7"
    )
    warned = Notification(
        spec="shop", job=JobKind.BUILD, outcome=Outcome.WARNING, generation="0193a7c21b4e9f30"
    )
    assert warned.text == "Datadouble shop: build passed with warnings, generation 0193a7c21b4e9f30"
    verified = Notification(spec="shop", job=JobKind.PROMOTE, outcome=Outcome.FAILURE)
    assert verified.text == "Datadouble shop: promote failed"
    succeeded = Notification(spec="shop", job=JobKind.FETCH, outcome=Outcome.SUCCESS)
    assert succeeded.text == "Datadouble shop: fetch succeeded"
    broken = Notification(
        spec="shop", job=JobKind.FETCH, outcome=Outcome.FAILURE, run_url="https://x.example\n/7"
    )
    assert broken.text == "Datadouble shop: fetch failed - https://x.example /7"
    healed = Notification(
        spec="shop", job=JobKind.BUILD, outcome=Outcome.SUCCESS, recovered=True, generation="0193"
    )
    assert healed.text == "Datadouble shop: build recovered, generation 0193"


def test_chat_webhooks_get_only_text(receiver: Receiver) -> None:
    chat = """
notify:
  to:
    - webhook:
        url:
          env: CHAT_WEBHOOK
        format: chat
    - webhook:
        url:
          env: ALERTS_WEBHOOK
"""
    spec = parse_spec(BASE + chat)
    assert spec.notify is not None
    assert [w.format for w in spec.notify.webhooks()] == [WebhookFormat.CHAT, WebhookFormat.JSON]
    dumped = spec.model_dump(mode="json", by_alias=True, exclude_none=True)["notify"]
    assert dumped["to"][0]["webhook"]["format"] == "chat"
    with pytest.raises(SpecError):
        parse_spec(BASE + chat.replace("format: chat", "format: slack"))
    environ = {"CHAT_WEBHOOK": receiver.url + "/chat", "ALERTS_WEBHOOK": receiver.url + "/json"}
    assert send(spec.notify, notification(), environ=environ) == ()
    first, second = receiver.received
    assert json.loads(first.body) == {"text": notification().text}
    assert json.loads(second.body)["text"] == notification().text
    assert set(json.loads(second.body)) > {"spec", "text"}


def test_tls_is_verified() -> None:
    context = tls_context()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname


def test_sending(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = parse_spec(BASE + NOTIFY)
    environ = {"ALERTS_WEBHOOK": receiver.url + "/hook"}
    assert send(spec.notify, notification(), environ=environ) == ()
    (got,) = receiver.received
    assert json.loads(got.body) == {
        "spec": "shop",
        "job": "build",
        "outcome": "failure",
        "recovered": False,
        "generation": "0193a7c21b4e9f30",
        "error": "store_failed",
        "run_url": "https://ci.example.com/runs/7",
        "text": "Datadouble shop: build failed (store_failed), generation 0193a7c21b4e9f30"
        " - https://ci.example.com/runs/7",
    }
    assert send(spec.notify, notification(Outcome.SUCCESS), environ=environ) == ()
    assert send(None, notification(), environ=environ) == ()
    assert len(receiver.received) == 1
    receiver.status = 404
    assert send(spec.notify, notification(), environ=environ) == (
        "webhook 1: the server answered with HTTP status 404",
    )
    assert send(spec.notify, notification(), environ={}) == (
        "webhook 1: environment variable ALERTS_WEBHOOK is not set",
    )
    refused = {"ALERTS_WEBHOOK": f"http://127.0.0.1:{closed_port()}/hook"}
    (problem,) = send(spec.notify, notification(), environ=refused)
    assert problem == "webhook 1: the request failed (ConnectionRefusedError)"
    assert refused["ALERTS_WEBHOOK"] not in problem
    bad_port = {"ALERTS_WEBHOOK": "http://127.0.0.1:port/hook"}
    assert send(spec.notify, notification(), environ=bad_port) == (
        "webhook 1: the request failed (ValueError)",
    )
    monkeypatch.setenv("ALERTS_WEBHOOK", receiver.url)
    receiver.status = 200
    assert send(spec.notify, notification(Outcome.WARNING)) == ()


def report(outcome: Outcome, **outputs: str) -> Report:
    return Report(
        command="run", outcome=outcome, outputs={"outcome": str(outcome), **outputs}, summary=""
    )


def test_requests_notify_for_their_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "shop.yml"
    path.write_text(BASE + NOTIFY, encoding="utf-8")
    monkeypatch.setenv("ALERTS_WEBHOOK", "https://hooks.example.com/x")
    posted: list[tuple[str, bytes]] = []

    def poster(url: str, body: bytes) -> None:
        posted.append((url, body))

    bridge = CiBridge(system="fake-ci", job=JobKind.FETCH)
    fetch = api.FetchRequest(command=api.Command.FETCH, spec=path, ci=bridge)
    failed = report(Outcome.FAILURE, error="staging_failed")
    assert api.notify(fetch, failed, "https://ci.example.com/7", poster=poster) == ()
    ((url, body),) = posted
    assert url == "https://hooks.example.com/x"
    assert json.loads(body) == {
        "spec": "shop",
        "job": "fetch",
        "outcome": "failure",
        "recovered": False,
        "generation": None,
        "error": "staging_failed",
        "run_url": "https://ci.example.com/7",
        "text": "Datadouble shop: fetch failed (staging_failed) - https://ci.example.com/7",
    }
    unnamed = fetch.model_copy(update={"ci": CiBridge(system="fake-ci")})
    assert api.notify(unnamed, failed, None, poster=poster) == ()
    alone = fetch.model_copy(update={"ci": None})
    assert api.notify(alone, failed, None, poster=poster) == ()
    version = api.VersionRequest(command=api.Command.VERSION, ci=bridge)
    assert api.notify(version, failed, None, poster=poster) == ()
    assert len(posted) == 1
    missing = fetch.model_copy(update={"spec": tmp_path / "missing.yml"})
    assert api.notify(missing, failed, None, poster=poster) == (
        "the spec cannot be read, so its webhooks were not posted to",
    )

    def broken(url: str, body: bytes) -> None:
        del url, body
        raise OSError

    def vague(url: str, body: bytes) -> None:
        del url, body
        reason = "unknown url type"
        raise urllib.error.URLError(reason)

    assert api.notify(fetch, failed, None, poster=vague) == (
        "webhook 1: the request failed (URLError)",
    )

    assert api.notify(fetch, failed, None, poster=broken) == (
        "webhook 1: the request failed (OSError)",
    )


RECOVERY = """
notify:
  when: [failure, recovery]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - webhook:
        url:
          env: CHAT_WEBHOOK
        format: chat
"""


def test_recovery_is_a_notify_outcome() -> None:
    spec = parse_spec(BASE + RECOVERY)
    assert spec.notify is not None
    assert spec.notify.when == (NotifyOn.FAILURE, NotifyOn.RECOVERY)
    assert spec.notify.recovers
    assert spec.notify.sends("success", recovered=True)
    assert not spec.notify.sends("success")
    failures = parse_spec(BASE + NOTIFY)
    assert failures.notify is not None
    assert not failures.notify.recovers
    assert not failures.notify.sends("success", recovered=True)
    mailed = parse_spec(BASE + "notify: {when: [recovery], to: [{email: {to: [a@example.com]}}]}")
    assert mailed.notify is not None
    assert not mailed.notify.recovers


def test_a_recovery_is_a_success_after_anything_else() -> None:
    assert recovered("failure", Outcome.SUCCESS)
    assert recovered("warning", Outcome.SUCCESS)
    assert recovered(RUNNING, Outcome.SUCCESS)
    assert not recovered("success", Outcome.SUCCESS)
    assert not recovered(None, Outcome.SUCCESS)
    assert not recovered("failure", Outcome.WARNING)
    assert not recovered("failure", Outcome.FAILURE)


def test_records(tmp_path: Path) -> None:
    path = tmp_path / "records" / "shop.build.json"
    assert recall(path) == (None, ())
    assert json.loads(path.read_text(encoding="utf-8")) == {"outcome": "running"}
    assert remember(path, "failure") == ()
    assert recall(path) == ("failure", ())
    assert [p.name for p in path.parent.iterdir()] == ["shop.build.json"]
    path.write_text("not json", encoding="utf-8")
    assert recall(path) == (None, ("the job's outcome record could not be read (ValidationError)",))
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    (problem,) = remember(blocked / "shop.build.json", "success")
    assert problem.startswith("the job's outcome record could not be written (")
    unreadable = tmp_path / "directory.json"
    unreadable.mkdir()
    previous, (reading, writing) = recall(unreadable)
    assert previous is None
    assert reading.startswith("the job's outcome record could not be read (")
    assert writing.startswith("the job's outcome record could not be written (")
    assert list(tmp_path.glob(".directory.json.*")) == []


def test_one_recovery_post_after_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "shop.yml"
    path.write_text(BASE + RECOVERY, encoding="utf-8")
    monkeypatch.setenv("ALERTS_WEBHOOK", "https://hooks.example.com/json")
    monkeypatch.setenv("CHAT_WEBHOOK", "https://chat.example.com/room")
    posted: list[tuple[str, bytes]] = []

    def poster(url: str, body: bytes) -> None:
        posted.append((url, body))

    record = tmp_path / "work" / ".datadouble-notify" / "shop.fetch.json"
    bridge = CiBridge(system="fake-ci", job=JobKind.FETCH, record=record)
    fetch = api.FetchRequest(command=api.Command.FETCH, spec=path, ci=bridge)

    def attempt(outcome: Outcome) -> tuple[str, ...]:
        previous, problems = api.previous_outcome(fetch)
        assert problems == ()
        assert json.loads(record.read_text(encoding="utf-8")) == {"outcome": "running"}
        return api.notify(fetch, report(outcome), None, previous=previous, poster=poster)

    assert attempt(Outcome.SUCCESS) == ()
    assert posted == []
    assert attempt(Outcome.FAILURE) == ()
    assert len(posted) == 2
    posted.clear()
    assert attempt(Outcome.SUCCESS) == ()
    (hook, hook_body), (chat, chat_body) = posted
    assert hook == "https://hooks.example.com/json"
    assert json.loads(hook_body) == {
        "spec": "shop",
        "job": "fetch",
        "outcome": "success",
        "recovered": True,
        "generation": None,
        "error": None,
        "run_url": None,
        "text": "Datadouble shop: fetch recovered",
    }
    assert chat == "https://chat.example.com/room"
    assert json.loads(chat_body) == {"text": "Datadouble shop: fetch recovered"}
    posted.clear()
    assert attempt(Outcome.SUCCESS) == ()
    assert posted == []
    # A run stopped before it ends leaves "running", which is not a success.
    api.previous_outcome(fetch)
    assert attempt(Outcome.SUCCESS) == ()
    assert len(posted) == 2
    assert json.loads(record.read_text(encoding="utf-8")) == {"outcome": "success"}
    unrecorded = fetch.model_copy(update={"ci": CiBridge(system="fake-ci", record=record)})
    assert api.previous_outcome(unrecorded) == (None, ())
    missing = fetch.model_copy(update={"spec": tmp_path / "missing.yml"})
    assert api.notify(missing, report(Outcome.FAILURE), None, poster=poster) == (
        "the spec cannot be read, so its webhooks were not posted to",
    )
    assert json.loads(record.read_text(encoding="utf-8")) == {"outcome": "failure"}
