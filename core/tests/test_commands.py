# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The lock, plan, run and schema commands, through the CLI and the JSON API."""

import io
import json
from pathlib import Path

import pytest
from test_run import SPEC, dataset

from datadouble import api, cli
from datadouble.api import ErrorCode, ExitCode
from datadouble.catalog import CatalogLock, write_lock
from datadouble.engine import EngineError
from datadouble.registry import Registry, RegistryError, installed
from datadouble.run import Run, RunError, RunOptions, Stage
from datadouble.spec import parse_spec
from datadouble.storage import StoreError, filesystem_store
from fake_engine import FakeEngine, FakeRuntime, check_protocols
from fakes import catalog, shop, table


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(catalog=shop(), source=dataset())


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "shop.yml"
    path.write_text(SPEC.replace("STORE", str(tmp_path / "store")), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def adapters(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> Registry:
    registry = Registry(
        engines={"fake": lambda: check_protocols(engine)},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: FakeRuntime()},
    )
    monkeypatch.setattr(api, "installed", lambda: registry)
    return registry


def cli_run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(""), stdout=out)
    return code, out.getvalue()


def test_schema() -> None:
    code, out = cli_run("schema")
    assert code == ExitCode.SUCCESS
    assert json.loads(out)["title"] == "Datadouble spec"
    code, out = cli_run("--json", "schema")
    assert json.loads(out)["schema"]["title"] == "Datadouble spec"


def test_lock_then_plan_then_run(spec_file: Path, tmp_path: Path) -> None:
    code, out = cli_run("lock", "--spec", str(spec_file))
    assert code == ExitCode.SUCCESS
    assert out.startswith(f"wrote {tmp_path / 'catalog.lock'}: 10 tables (sha256:")

    code, out = cli_run("plan", "--spec", str(spec_file))
    assert code == ExitCode.SUCCESS
    assert out.splitlines()[0].startswith("plan for shop: 4 tables, 6 excluded, strategy nested")
    assert out.splitlines()[1] == "  image all: 4 tables"

    work = tmp_path / "work"
    code, out = cli_run("run", "plan,extract", "--spec", str(spec_file), "--work", str(work))
    assert code == ExitCode.SUCCESS
    generation = out.split()[1]
    assert "  plan: ran in" in out

    code, out = cli_run(
        "--json",
        "run",
        ",".join(Stage),
        "--spec",
        str(spec_file),
        "--work",
        str(work),
        "--resume",
        generation,
    )
    response = json.loads(out)
    assert code == ExitCode.SUCCESS
    assert response["result"]["generation"] == generation
    code, out = cli_run(
        "run",
        ",".join(Stage),
        "--spec",
        str(spec_file),
        "--work",
        str(work),
        "--resume",
        generation,
    )
    assert "  verify [all]: passed" in out
    assert "  stored filesystem:" in out


def test_plan_warnings(spec_file: Path) -> None:
    code, out = cli_run("plan", "--spec", str(spec_file), "--no-strict")
    assert code == ExitCode.WARNING
    assert "warning: no catalog lock" in out
    code, out = cli_run(
        "--json",
        "plan",
        "--spec",
        str(spec_file),
        "--strict",
        "off",
        "--strategy",
        "keys",
        "--format",
        "logical",
    )
    plan = json.loads(out)["plan"]
    assert (plan["strategy"], plan["formats"]) == ("keys", ["logical"])


def test_run_warnings_and_failures(spec_file: Path, tmp_path: Path) -> None:
    work = str(tmp_path / "work")
    code, out = cli_run(
        "run",
        "plan,extract,load,transform,package,verify,store",
        "--spec",
        str(spec_file),
        "--work",
        work,
        "--no-strict",
    )
    assert code == ExitCode.WARNING
    assert "warning: no catalog lock" in out
    spec_file.write_text(spec_file.read_text().replace("anonymize:", "anon_off:"), encoding="utf-8")
    code, out = cli_run("run", "plan,verify", "--spec", str(spec_file), "--work", work)
    assert code == ExitCode.FAILURE
    assert "invalid_spec" not in out


def test_verification_failure_summary(spec_file: Path, tmp_path: Path) -> None:
    text = spec_file.read_text()
    text = text.replace("  users: {email: {faker: email, unique: true}, name: {faker: name}}\n", "")
    text = text.replace("  comments: {body: {expression: \"'redacted'\"}}\n", "").replace(
        "anonymize:\n", ""
    )
    spec_file.write_text(text, encoding="utf-8")
    write_lock(CatalogLock.of(shop()), tmp_path / "catalog.lock")
    code, out = cli_run(
        "run", ",".join(Stage), "--spec", str(spec_file), "--work", str(tmp_path / "w")
    )
    assert code == ExitCode.FAILURE
    assert "  verify [all]: FAILED" in out
    assert "    pii-scan public.users.email: 3 value(s) look like email" in out


def test_strict_failures_explain_how_to_resolve(
    spec_file: Path, tmp_path: Path, engine: FakeEngine
) -> None:
    write_lock(CatalogLock.of(shop()), tmp_path / "catalog.lock")
    engine.catalog = catalog(*shop().tables, table("secrets", ["id"]))
    code, out = cli_run("plan", "--spec", str(spec_file))
    assert code == ExitCode.FAILURE
    assert "error: the plan has 1 problem" in out
    assert "  to resolve public.secrets:" in out
    assert "    - exclude it" in out
    response = api.handle_json(json.dumps({"command": "plan", "spec": str(spec_file)}))
    assert isinstance(response, api.ErrorResponse)
    assert response.strict is not None


def test_dry_run_builds_complete_requests() -> None:
    code, out = cli_run(
        "--dry-run",
        "run",
        "plan, extract",
        "--spec",
        "s.yml",
        "--lock",
        "l.lock",
        "--strict",
        "tables",
        "--strategy",
        "nested",
        "--format",
        "physical",
        "--work",
        "w",
        "--resume",
        "g",
    )
    assert code == ExitCode.SUCCESS
    assert json.loads(out) == {
        "command": "run",
        "spec": "s.yml",
        "lock": "l.lock",
        "strict": "tables",
        "strategy": "nested",
        "formats": ["physical"],
        "stages": ["plan", "extract"],
        "work": "w",
        "resume": "g",
    }
    assert isinstance(api.parse_request(out), api.RunRequest)


def test_minimal_dry_run() -> None:
    _, out = cli_run("--dry-run", "run", "plan", "--spec", "s.yml")
    assert json.loads(out) == {"command": "run", "spec": "s.yml", "stages": ["plan"]}


def test_removed_tables_have_no_resolutions(spec_file: Path, tmp_path: Path) -> None:
    write_lock(
        CatalogLock.of(catalog(*shop().tables, table("gone", ["id"]))), tmp_path / "catalog.lock"
    )
    code, out = cli_run("plan", "--spec", str(spec_file))
    assert code == ExitCode.FAILURE
    assert "table-removed public.gone" in out
    assert "to resolve" not in out


def test_bad_stage_lists_are_usage_errors() -> None:
    for stages in ("plan,nope", ","):
        with pytest.raises(SystemExit) as caught:
            cli_run("run", stages, "--spec", "x")
        assert caught.value.code == ExitCode.USAGE


@pytest.mark.parametrize(
    ("raw", "location"),
    [
        ('{"command": "run", "spec": "x", "stages": []}', "stages"),
        ('{"command": "plan", "spec": "x", "formats": []}', "formats"),
        ('{"command": "lock"}', "spec"),
    ],
)
def test_request_validation(raw: str, location: str) -> None:
    response = api.handle_json(raw)
    assert isinstance(response, api.ErrorResponse)
    assert [d.location for d in response.details] == [location]


def test_errors_map_to_codes(spec_file: Path, tmp_path: Path) -> None:
    missing = api.handle_json(json.dumps({"command": "plan", "spec": str(tmp_path / "nope.yml")}))
    assert isinstance(missing, api.ErrorResponse)
    assert missing.error is ErrorCode.INVALID_SPEC
    bad = tmp_path / "bad.yml"
    bad.write_text("name: shop\nsource: {engine: nope}\n", encoding="utf-8")
    response = api.handle_json(json.dumps({"command": "lock", "spec": str(bad)}))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is ErrorCode.ADAPTER_MISSING
    lock = tmp_path / "broken.lock"
    lock.write_text("{}", encoding="utf-8")
    response = api.handle_json(
        json.dumps({"command": "plan", "spec": str(spec_file), "lock": str(lock)})
    )
    assert isinstance(response, api.ErrorResponse)
    assert response.error is ErrorCode.CATALOG_LOCK
    invalid = api.handle_json(json.dumps({"command": "version", "x": 1}))
    assert isinstance(invalid, api.ErrorResponse)
    assert "Extra inputs" in cli.summarize(invalid)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (EngineError("x"), ErrorCode.ENGINE_FAILED),
        (StoreError("x"), ErrorCode.STORE_FAILED),
        (RunError("x"), ErrorCode.RUN_FAILED),
        (RegistryError("x"), ErrorCode.ADAPTER_MISSING),
    ],
)
def test_run_errors(
    error: Exception,
    code: ErrorCode,
    spec_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_self: Run) -> None:
        raise error

    monkeypatch.setattr(Run, "execute", explode)
    request = {
        "command": "run",
        "spec": str(spec_file),
        "stages": ["plan"],
        "work": str(tmp_path / "work"),
    }
    response = api.handle_json(json.dumps(request))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is code
    assert response.message == "x"


def test_settings_errors(spec_file: Path, tmp_path: Path) -> None:
    write_lock(CatalogLock.of(shop()), tmp_path / "catalog.lock")
    spec_file.write_text(
        spec_file.read_text().replace("filesystem: {path:", "filesystem: {route:"), encoding="utf-8"
    )
    response = api.handle_json(
        json.dumps(
            {
                "command": "run",
                "spec": str(spec_file),
                "stages": list(Stage),
                "work": str(tmp_path / "w"),
            }
        )
    )
    assert isinstance(response, api.ErrorResponse)
    assert response.error is ErrorCode.INVALID_SETTINGS
    assert "path: Field required" in response.message


def test_installed_registry() -> None:
    registry = installed()
    assert "filesystem" in registry.stores
    with pytest.raises(RegistryError, match="no engine adapter named 'nope'"):
        Registry(engines={}, stores={}).engine("nope")
    with pytest.raises(RegistryError, match="no store adapter named 'nope'"):
        Registry(engines={}, stores={}).store("nope", {})


def test_required_stage(tmp_path: Path, adapters: Registry) -> None:
    run = Run(parse_spec(SPEC), None, adapters, RunOptions(stages=(Stage.PLAN,), work=tmp_path))
    with pytest.raises(RunError, match="stage verify has not completed"):
        run.required(Stage.VERIFY, None)
