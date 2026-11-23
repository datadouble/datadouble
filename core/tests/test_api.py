# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
import platform
from pathlib import Path

import pytest

from datadouble import __version__, api
from datadouble.engine import RequirementError
from datadouble.outcome import ExitCode


def test_version_request_returns_installed_version() -> None:
    response = api.handle(api.VersionRequest(command=api.Command.VERSION))

    assert response == api.VersionResponse(version=__version__, python=platform.python_version())
    assert api.exit_code(response) is ExitCode.SUCCESS


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
    assert api.exit_code(response) is ExitCode.FAILURE


def test_messages_are_immutable() -> None:
    request = api.VersionRequest(command=api.Command.VERSION)

    with pytest.raises(ValueError, match="frozen"):
        request.command = api.Command.VERSION


def test_file_failures_are_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(path: Path) -> object:
        raise PermissionError(13, "Permission denied", str(path / "customers" / "a-name.pdf"))

    monkeypatch.setattr(api, "load_spec", unreadable)
    response = api.handle(api.FetchRequest(command=api.Command.FETCH, spec=tmp_path / "s.yml"))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is api.ErrorCode.FILE_FAILED
    assert response.message == "a file operation failed: Permission denied"


def test_unmet_requirements_are_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def lacking(_path: Path) -> object:
        raise RequirementError((("warehouse", "extension vector is not in the working image"),))

    monkeypatch.setattr(api, "load_spec", lacking)
    response = api.handle(api.FetchRequest(command=api.Command.FETCH, spec=tmp_path / "s.yml"))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is api.ErrorCode.REQUIREMENT_UNMET
    assert "sources.<name>.image" in response.message
    assert [(d.location, d.problem) for d in response.details] == [
        ("sources.warehouse.image", "extension vector is not in the working image")
    ]
