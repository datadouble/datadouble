# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The command-line interface.

The CLI only builds JSON requests for :mod:`datadouble.api`. ``--dry-run``
prints the request it would send, and ``datadouble api`` reads a request from
standard input, so anything the CLI can do is also available as JSON.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO, assert_never

from datadouble import api


class _Arguments(argparse.Namespace):
    """Typed view of the parsed command line."""

    command: str
    json: bool
    dry_run: bool


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="datadouble",
        description="Realistic, safe copies of production data for development and testing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the JSON response instead of a summary",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the JSON request without running it",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser(api.Command.VERSION, help="show the installed version")
    commands.add_parser("api", help="read a JSON request from standard input")
    return parser


_REQUESTS: dict[str, Callable[[_Arguments], api.Request]] = {
    api.Command.VERSION: lambda _: api.VersionRequest(command=api.Command.VERSION),
}


def build_request(arguments: _Arguments) -> api.Request:
    """Turn parsed command-line arguments into a JSON request.

    The parser only accepts commands listed here, so the lookup cannot miss.
    """
    return _REQUESTS[arguments.command](arguments)


def summarize(response: api.Response) -> str:
    """Describe a response for people, the default output without ``--json``."""
    match response:
        case api.VersionResponse():
            return f"datadouble {response.version} (Python {response.python})"
        case api.ErrorResponse():
            lines = [f"error: {response.message}"]
            lines.extend(f"  {detail.location}: {detail.problem}" for detail in response.details)
            return "\n".join(lines)
        case _:
            assert_never(response)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the command line and return the process exit code."""
    source: TextIO = sys.stdin if stdin is None else stdin
    sink: TextIO = sys.stdout if stdout is None else stdout
    arguments = build_parser().parse_args(argv, namespace=_Arguments())

    if arguments.command == "api":
        response = api.handle_json(source.read())
        sink.write(response.model_dump_json() + "\n")
        return api.exit_code(response)

    request = build_request(arguments)
    if arguments.dry_run:
        sink.write(request.model_dump_json() + "\n")
        return api.ExitCode.SUCCESS

    response = api.handle(request)
    sink.write((response.model_dump_json() if arguments.json else summarize(response)) + "\n")
    return api.exit_code(response)


if __name__ == "__main__":
    sys.exit(main())
