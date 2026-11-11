# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The non-production commands through the CLI and the JSON API."""

import io
import json
from pathlib import Path

import pytest
from test_operations import build, make_spec

from datadouble import api, cli
from datadouble.api import ErrorCode, ExitCode
from datadouble.registry import Registry
from datadouble.spec import SpecError, parse_spec


@pytest.fixture
def spec_file(tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(api, "installed", lambda: registry)
    path = tmp_path / "shop.yml"
    spec = make_spec(tmp_path, mode="on-demand", size=1)
    text = spec.model_dump_json(by_alias=True, exclude_none=True)
    path.write_text(text, encoding="utf-8")
    assert parse_spec(text) == spec
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(""), stdout=out)
    return code, out.getvalue()


def test_the_whole_non_production_side(spec_file: Path, tmp_path: Path) -> None:
    build(tmp_path, "0000000000010001")
    spec = ("--spec", str(spec_file))
    code, out = run("promote", *spec)
    assert code == ExitCode.SUCCESS
    assert out.startswith(
        "promoted shop/all/fake-18/0000000000010001 to channel latest in filesystem:"
    )
    code, out = run("fetch", *spec, "--group", "all")
    assert out == "shop/all/fake-18: fetched 0000000000010001\n"
    code, out = run("replicate", *spec, "--owner", "mr-7")
    assert code == ExitCode.SUCCESS
    assert out.startswith("replicate: 1 copies recorded\n  made ")
    assert f"  announced to dotenv:{tmp_path / 'clone.env'}" in out
    code, out = run("--json", "copies", *spec)
    (copy,) = json.loads(out)["result"]["copies"]
    assert copy["secrets"] == {}
    code, out = run("announce", *spec, "--owner", "mr-7")
    assert "  claimed " in out
    code, out = run("release", copy["id"], *spec)
    assert "  removed " in out
    assert f"  withdrew dotenv:{tmp_path / 'clone.env'}" in out
    code, out = run("activate", "0000000000010001", *spec)
    assert out == "channel active now points at 0000000000010001\n"
    code, out = run("prune", *spec, "--target", "staging", "--dry-run")
    assert out == "prune: would remove 0 bytes\n"
    code, out = run("prune", *spec, "--target", "store")
    assert out == "prune: removed 0 bytes\n"


def test_pools_and_claims(spec_file: Path, tmp_path: Path) -> None:
    pooled = spec_file.read_text().replace('"mode":"on-demand"', '"mode":"pool"')
    spec_file.write_text(pooled, encoding="utf-8")
    build(tmp_path, "0000000000010001")
    spec = ("--spec", str(spec_file))
    run("promote", *spec)
    run("fetch", *spec)
    code, out = run("replicate", *spec, "--no-announce", "--all-at-once")
    assert "announced" not in out
    code, out = run("claim", *spec, "--owner", "dev")
    assert code == ExitCode.SUCCESS
    assert out.splitlines()[1].startswith("  claimed ")
    assert "  announced to dotenv:" in out
    code, out = run("--json", "claim", *spec, "--owner", "dev2", "--no-announce")
    assert json.loads(out)["error"] == "replicate_failed"
    code, out = run("announce", *spec)
    assert "  ready " not in out


def test_dry_runs_build_requests() -> None:
    cases = {
        ("promote", "--spec", "s", "--group", "a", "--generation", "g"): {
            "command": "promote",
            "spec": "s",
            "groups": ["a"],
            "generation": "g",
        },
        ("replicate", "--spec", "s", "--owner", "o", "--all-at-once", "--no-announce"): {
            "command": "replicate",
            "spec": "s",
            "owner": "o",
            "all_at_once": True,
            "announce": False,
        },
        ("release", "a", "b", "--spec", "s", "--expired"): {
            "command": "release",
            "spec": "s",
            "copy_ids": ["a", "b"],
            "expired": True,
        },
        ("prune", "--spec", "s", "--target", "export", "--dry-run"): {
            "command": "prune",
            "spec": "s",
            "target": "export",
            "dry_run": True,
        },
        ("claim", "--spec", "s", "--owner", "o"): {"command": "claim", "spec": "s", "owner": "o"},
    }
    for argv, expected in cases.items():
        _code, out = run("--dry-run", *argv)
        assert json.loads(out) == expected
        assert not isinstance(api.parse_request(out), api.ErrorResponse)


def test_refusals_and_errors(spec_file: Path, tmp_path: Path) -> None:
    build(tmp_path, "0000000000010001", signed=False)
    code, out = run("promote", "--spec", str(spec_file))
    assert code == ExitCode.FAILURE
    assert out.startswith("error: promote refused: 1 problem\n  promote: ")
    response = api.handle_json(json.dumps({"command": "promote", "spec": str(spec_file)}))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is ErrorCode.PROMOTE_REFUSED
    for request, code_ in (
        ({"command": "fetch", "spec": str(spec_file)}, ErrorCode.STAGING_FAILED),
        ({"command": "release", "spec": str(spec_file)}, ErrorCode.REPLICATE_FAILED),
        ({"command": "prune", "spec": str(spec_file), "target": "export", "dry_run": True}, None),
    ):
        response = api.handle_json(json.dumps(request))
        assert getattr(response, "error", None) is code_


def test_spec_validation() -> None:
    base = "name: shop\nsource: {engine: fake}\n"
    for extra, problem in (
        ("announce: [{a: {}, b: {}}]", "exactly one adapter"),
        ("signing: {cosign: {}}", "exactly one of `key` or `keyless"),
        ("signing: {cosign: {keyless: true, key: {env: K}}}", "exactly one of `key`"),
        ("trust: {cosign: {identity: me}}", "both `identity` and `issuer`"),
        ("trust: {cosign: {public_key: {env: P}, identity: a, issuer: b}}", "either `public_key`"),
        ("promote: {to: [{x: {}}], channel: Latest}", "lowercase"),
        ("fetch: {from: {x: {}}, staging: /s, channel: Bad}", "lowercase"),
    ):
        with pytest.raises(SpecError) as caught:
            parse_spec(base + extra)
        assert any(problem in message for _, message in caught.value.details), caught.value.details
