# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
import platform

import pytest

from datadouble import __version__, api


def test_version_request_returns_installed_version() -> None:
    response = api.handle(api.VersionRequest(command=api.Command.VERSION))

    assert response == api.VersionResponse(version=__version__, python=platform.python_version())
    assert api.exit_code(response) is api.ExitCode.SUCCESS


def test_json_request_round_trip() -> None:
    response = api.handle_json('{"command": "version"}')

    assert isinstance(response, api.VersionResponse)
    assert json.loads(response.model_dump_json()) == {
        "command": "version",
        "outcome": "success",
        "version": __version__,
        "python": platform.python_version(),
    }


@pytest.mark.parametrize(
    ("raw", "location"),
    [
        ('{"command": "frobnicate"}', "command"),
        ('{"command": "version", "extra": true}', "extra"),
        ("{}", "command"),
        ("not json", "(request)"),
    ],
)
def test_invalid_request_fails_loudly(raw: str, location: str) -> None:
    response = api.handle_json(raw)

    assert isinstance(response, api.ErrorResponse)
    assert response.error is api.ErrorCode.INVALID_REQUEST
    assert location in [detail.location for detail in response.details]
    assert api.exit_code(response) is api.ExitCode.FAILURE


def test_messages_are_immutable() -> None:
    request = api.VersionRequest(command=api.Command.VERSION)

    with pytest.raises(ValueError, match="frozen"):
        request.command = api.Command.VERSION
