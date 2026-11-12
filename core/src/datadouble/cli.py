# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The command-line interface.

The CLI only builds JSON requests for :mod:`datadouble.api`. ``--dry-run``
prints the request it would send, and ``datadouble api`` reads a request from
standard input, so anything the CLI can do is also available as JSON.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import TextIO, assert_never

from pydantic import JsonValue

from datadouble import api
from datadouble.prune import PruneTarget
from datadouble.replicate import Copy
from datadouble.run import Stage
from datadouble.spec import ImageFormat, Strategy, StrictLevel


class _Arguments(argparse.Namespace):
    """Typed view of the parsed command line."""

    command: str
    json: bool
    dry_run: bool
    spec: str | None
    lock: str | None
    strict: str | None
    strategy: str | None
    formats: list[str] | None
    stages: list[str]
    work: str | None
    resume: str | None
    groups: list[str] | None
    generation: str | None
    owner: str | None
    all_at_once: bool
    no_announce: bool
    copy_ids: list[str]
    model: str
    datamodel: str
    output: str
    expired: bool
    target: str
    report_only: bool


def _stages(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    known = {stage.value for stage in Stage}
    unknown = [name for name in names if name not in known]
    if unknown or not names:
        message = f"stages are a comma-separated list of: {', '.join(Stage)}"
        raise argparse.ArgumentTypeError(message)
    return names


def _spec_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", required=True, help="the spec file (YAML or JSON)")
    parser.add_argument("--lock", help="the catalog lock (default: catalog.lock beside the spec)")


def _override_arguments(parser: argparse.ArgumentParser) -> None:
    strictness = parser.add_mutually_exclusive_group()
    strictness.add_argument(
        "--strict", choices=[level.value for level in StrictLevel], help="override strict mode"
    )
    strictness.add_argument(
        "--no-strict",
        dest="strict",
        action="store_const",
        const=StrictLevel.OFF.value,
        help="shorthand for --strict=off",
    )
    parser.add_argument(
        "--strategy", choices=[s.value for s in Strategy], help="override the subset strategy"
    )
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=[f.value for f in ImageFormat],
        help="an image format to produce (repeatable); overrides package.formats",
    )


def _group_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", required=True, help="the spec file (YAML or JSON)")
    parser.add_argument(
        "--group",
        dest="groups",
        action="append",
        help="an image group to work on (repeatable); every group by default",
    )


def _operation_parsers(add: Callable[..., argparse.ArgumentParser]) -> None:
    promote = add(api.Command.PROMOTE, help="copy verified, signed images to the export stores")
    _group_arguments(promote)
    promote.add_argument("--generation", help="the generation to promote (default: newest)")
    fetch = add(api.Command.FETCH, help="bring new promoted images into staging")
    _group_arguments(fetch)
    activate = add(api.Command.ACTIVATE, help="point the staging channel at a staged generation")
    activate.add_argument("generation", help="the generation to activate")
    _group_arguments(activate)
    replicate = add(
        api.Command.REPLICATE, help="fill the pool, or make on-demand copies, and announce them"
    )
    _group_arguments(replicate)
    replicate.add_argument("--owner", help="who on-demand copies are for")
    replicate.add_argument(
        "--all-at-once", action="store_true", help="replace stale pool copies before refilling"
    )
    replicate.add_argument("--no-announce", action="store_true", help="make copies only")
    claim = add(api.Command.CLAIM, help="take an unclaimed pool copy")
    _group_arguments(claim)
    claim.add_argument("--owner", required=True, help="who is taking the copy")
    claim.add_argument("--no-announce", action="store_true", help="claim only")
    release = add(api.Command.RELEASE, help="destroy copies by ID, owner or expiry")
    release.add_argument("copy_ids", nargs="*", metavar="COPY_ID", help="copies to release")
    _group_arguments(release)
    release.add_argument("--owner", help="release every copy this owner has")
    release.add_argument("--expired", action="store_true", help="release expired copies")
    announce = add(api.Command.ANNOUNCE, help="announce existing copies again")
    _group_arguments(announce)
    announce.add_argument("--owner", help="announce this owner's copies (default: the pool)")
    copies = add(api.Command.COPIES, help="list copies")
    _group_arguments(copies)
    prune = add(api.Command.PRUNE, help="apply retention")
    prune.add_argument("--spec", required=True, help="the spec file (YAML or JSON)")
    prune.add_argument(
        "--target",
        required=True,
        choices=[target.value for target in PruneTarget],
        help="the build's stores, the export stores, or staging",
    )
    prune.add_argument(
        "--dry-run",
        dest="report_only",
        action="store_true",
        help="report what would be removed, and the space recovered",
    )


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
    commands.add_parser(api.Command.SCHEMA, help="print the spec JSON Schema")
    lock = commands.add_parser(api.Command.LOCK, help="write the catalog lock for a spec")
    _spec_arguments(lock)
    importing = commands.add_parser(
        api.Command.IMPORT_JAILER,
        help="translate a Jailer subset into native spec settings, and lock the catalog",
    )
    _spec_arguments(importing)
    importing.add_argument("--model", required=True, help="the Jailer extraction model (.jm)")
    importing.add_argument("--datamodel", required=True, help="the Jailer data model folder")
    importing.add_argument(
        "--output", required=True, help="where to write the native subset settings (YAML)"
    )
    planning = commands.add_parser(api.Command.PLAN, help="plan a build without extracting")
    _spec_arguments(planning)
    _override_arguments(planning)
    run = commands.add_parser(api.Command.RUN, help="run build stages, or resume a generation")
    run.add_argument("stages", type=_stages, help=f"comma-separated stages: {','.join(Stage)}")
    _spec_arguments(run)
    _override_arguments(run)
    run.add_argument("--work", help="the work directory (default: .datadouble)")
    run.add_argument("--resume", metavar="GENERATION", help="resume this generation")
    _operation_parsers(commands.add_parser)
    commands.add_parser("api", help="read a JSON request from standard input")
    return parser


def _spec_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {"spec": arguments.spec}
    if arguments.lock:
        fields["lock"] = arguments.lock
    return fields


def _override_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields = _spec_fields(arguments)
    if arguments.strict:
        fields["strict"] = arguments.strict
    if arguments.strategy:
        fields["strategy"] = arguments.strategy
    if arguments.formats:
        fields["formats"] = list(arguments.formats)
    return fields


def _run_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields = _override_fields(arguments)
    fields["stages"] = list(arguments.stages)
    if arguments.work:
        fields["work"] = arguments.work
    if arguments.resume:
        fields["resume"] = arguments.resume
    return fields


def _group_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {"spec": arguments.spec}
    if arguments.groups:
        fields["groups"] = list(arguments.groups)
    return fields


def _set(fields: dict[str, JsonValue], arguments: _Arguments, *names: str) -> dict[str, JsonValue]:
    """Copy the named arguments that were given into the request."""
    for name in names:
        value: JsonValue = getattr(arguments, name, None)
        if value:
            fields[name] = list(value) if isinstance(value, list) else value
    return fields


def _promote_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    return _set(_group_fields(arguments), arguments, "generation")


def _activate_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    return _set(_group_fields(arguments), arguments, "generation")


def _replicate_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields = _set(_group_fields(arguments), arguments, "owner", "all_at_once")
    if arguments.no_announce:
        fields["announce"] = False
    return fields


def _claim_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields = _set(_group_fields(arguments), arguments, "owner")
    if arguments.no_announce:
        fields["announce"] = False
    return fields


def _release_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    return _set(_group_fields(arguments), arguments, "copy_ids", "owner", "expired")


def _announce_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    return _set(_group_fields(arguments), arguments, "owner")


def _prune_fields(arguments: _Arguments) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {"spec": arguments.spec, "target": arguments.target}
    if arguments.report_only:
        fields["dry_run"] = True
    return fields


_FIELDS: dict[str, Callable[[_Arguments], dict[str, JsonValue]]] = {
    api.Command.VERSION: lambda _: {},
    api.Command.SCHEMA: lambda _: {},
    api.Command.LOCK: _spec_fields,
    api.Command.IMPORT_JAILER: lambda arguments: {
        **_spec_fields(arguments),
        "model": arguments.model,
        "datamodel": arguments.datamodel,
        "output": arguments.output,
    },
    api.Command.PLAN: _override_fields,
    api.Command.RUN: _run_fields,
    api.Command.PROMOTE: _promote_fields,
    api.Command.FETCH: _group_fields,
    api.Command.ACTIVATE: _activate_fields,
    api.Command.REPLICATE: _replicate_fields,
    api.Command.CLAIM: _claim_fields,
    api.Command.RELEASE: _release_fields,
    api.Command.ANNOUNCE: _announce_fields,
    api.Command.COPIES: _group_fields,
    api.Command.PRUNE: _prune_fields,
}


def build_request(arguments: _Arguments) -> str:
    """Turn parsed command-line arguments into a JSON request.

    The parser only accepts commands listed here, so the lookup cannot miss.
    """
    return json.dumps({"command": arguments.command, **_FIELDS[arguments.command](arguments)})


def _error_lines(response: api.ErrorResponse) -> list[str]:
    lines = [f"error: {response.message}"]
    lines.extend(f"  {detail.location}: {detail.problem}" for detail in response.details)
    if response.strict is not None:
        for finding in response.strict.findings:
            if finding.resolutions:
                lines.append(f"  to resolve {finding.object}:")
                lines.extend(f"    - {way}" for way in finding.resolutions)
    return lines


def _plan_lines(response: api.PlanResponse) -> list[str]:
    planned = response.plan
    excluded = len(planned.excludes.excluded())
    lines = [
        (
            f"plan for {planned.spec}: {len(planned.extracted())} tables, {excluded} excluded, "
            f"strategy {planned.strategy}, as of {planned.as_of.isoformat()}"
        ),
    ]
    lines.extend(f"  image {g.name}: {len(g.tables)} tables" for g in planned.groups)
    lines.extend(f"warning: {w}" for w in planned.warnings)
    return lines


def _run_lines(response: api.RunResponse) -> list[str]:
    result = response.result
    lines = [f"generation {result.generation} ({response.outcome}), work in {result.work}"]
    for record in result.stages:
        where = f" [{record.group}]" if record.group else ""
        lines.append(f"  {record.stage}{where}: {record.status} in {record.seconds:.1f}s")
    for report in result.verification:
        verdict = "passed" if report.passed() else "FAILED"
        lines.append(f"  verify [{report.group}]: {verdict}")
        lines.extend(
            f"    {check.kind} {check.name}: {check.detail}"
            for check in report.checks()
            if not check.passed
        )
    lines.extend(f"  stored {key}" for key in result.stored)
    lines.extend(f"warning: {w}" for w in result.warnings)
    return lines


def _copy_line(copy: Copy, verb: str) -> str:
    where = ", ".join(f"{k}={v}" for k, v in copy.location.items())
    owner = f" for {copy.owner}" if copy.owner else ""
    return f"  {verb} {copy.id} ({copy.kind}, {copy.generation}{owner}) {where}".rstrip()


def _operation_lines(
    response: api.PromoteResponse
    | api.FetchResponse
    | api.ActivateResponse
    | api.CopiesResponse
    | api.PruneResponse,
) -> list[str]:
    match response:
        case api.PromoteResponse():
            return [
                f"promoted {p.summary.identity.prefix()}/{p.summary.identity.generation} "
                f"to channel {p.channel} in {', '.join(p.stores)}"
                for p in response.promoted
            ]
        case api.FetchResponse():
            return [f"{f.prefix}: {f.status} {f.generation}" for f in response.fetched]
        case api.ActivateResponse():
            return [f"channel {c.name} now points at {c.generation}" for c in response.channels]
        case api.CopiesResponse():
            result = response.result
            made = "claimed" if response.command is api.Command.CLAIM else "made"
            lines = [_copy_line(c, made) for c in result.created]
            lines.extend(_copy_line(c, "removed") for c in result.removed)
            if response.command in {api.Command.COPIES, api.Command.ANNOUNCE}:
                lines.extend(
                    _copy_line(c, "claimed" if c.claimed else "ready") for c in result.copies
                )
            lines.extend(f"  announced to {target}" for target in response.announced)
            lines.extend(f"  withdrew {target}" for target in response.withdrawn)
            return [f"{response.command}: {len(result.copies)} copies recorded", *lines]
        case api.PruneResponse():
            verb = "would remove" if response.result.dry_run else "removed"
            lines = [
                f"  {verb} {p.prefix}/{p.generation} from {p.location} ({p.bytes} bytes)"
                for p in response.result.removed
            ]
            lines.extend(f"  {verb} copy {c}" for c in response.result.copies_removed)
            return [f"prune: {verb} {response.bytes} bytes", *lines]
        case _:
            assert_never(response)


def _simple(response: api.VersionResponse | api.SchemaResponse | api.LockResponse) -> str:
    match response:
        case api.VersionResponse():
            return f"datadouble {response.version} (Python {response.python})"
        case api.SchemaResponse():
            return json.dumps(response.schema_, indent=2)
        case api.LockResponse():
            return f"wrote {response.lock}: {response.tables} tables ({response.fingerprint})"
        case _:
            assert_never(response)


def summarize(response: api.Response) -> str:
    """Describe a response for people, the default output without ``--json``."""
    match response:
        case api.VersionResponse() | api.SchemaResponse() | api.LockResponse():
            return _simple(response)
        case api.ImportJailerResponse():
            lines = [f"wrote {response.output} and {response.lock}"]
            lines.extend(f"  {line}" for line in response.report)
            return "\n".join(lines)
        case api.PlanResponse():
            return "\n".join(_plan_lines(response))
        case api.RunResponse():
            return "\n".join(_run_lines(response))
        case api.ErrorResponse():
            return "\n".join(_error_lines(response))
        case (
            api.PromoteResponse()
            | api.FetchResponse()
            | api.ActivateResponse()
            | api.CopiesResponse()
            | api.PruneResponse()
        ):
            return "\n".join(_operation_lines(response))
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
        sink.write(request + "\n")
        return api.ExitCode.SUCCESS

    response = api.handle_json(request)
    sink.write((response.model_dump_json() if arguments.json else summarize(response)) + "\n")
    return api.exit_code(response)


if __name__ == "__main__":
    sys.exit(main())
