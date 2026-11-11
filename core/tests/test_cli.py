# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import io
import json
import platform

import pytest

from datadouble import __version__, api, cli
from datadouble.api import ExitCode


def run(*argv: str, stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out)
    return code, out.getvalue()


def test_version_summary() -> None:
    code, out = run("version")

    assert code == ExitCode.SUCCESS
    assert out == f"datadouble {__version__} (Python {platform.python_version()})\n"


def test_version_json() -> None:
    code, out = run("--json", "version")

    assert code == ExitCode.SUCCESS
    assert json.loads(out)["version"] == __version__


def test_dry_run_prints_the_request_only() -> None:
    code, out = run("--dry-run", "version")

    assert code == ExitCode.SUCCESS
    assert json.loads(out) == {"command": "version"}


def test_dry_run_output_is_a_valid_api_request() -> None:
    _, request = run("--dry-run", "version")
    code, out = run("api", stdin=request)

    assert code == ExitCode.SUCCESS
    assert json.loads(out)["outcome"] == "success"


def test_api_rejects_invalid_request() -> None:
    code, out = run("api", stdin='{"command": "nope"}')

    assert code == ExitCode.FAILURE
    assert json.loads(out)["error"] == "invalid_request"


def test_error_summary_lists_each_problem() -> None:
    response = api.handle_json('{"command": "nope"}')

    assert cli.summarize(response).startswith("error: The request is not a valid")


def test_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        run("frobnicate")

    assert exit_info.value.code == ExitCode.USAGE


def test_defaults_to_process_streams(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--dry-run", "version"]) == ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out) == {"command": "version"}
