# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Generating CI/CD jobs, and bridging requests to a CI system."""

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
from test_operations import make_spec
from test_run import SPEC, dataset

from datadouble import __version__, api, cli
from datadouble.api import ErrorCode
from datadouble.boundary import BoundaryError, require_inside
from datadouble.ci import (
    CiError,
    GeneratedFile,
    JobKind,
    Pipeline,
    PipelineOptions,
    Placeholder,
    Report,
    Side,
    Trigger,
    host_paths,
    mask,
    masking,
    pipeline,
    secret_names,
    side_of,
)
from datadouble.outcome import ExitCode, Outcome
from datadouble.registry import Registry
from datadouble.runtime import identity
from datadouble.secrets import resolve_secret
from datadouble.spec import EnvSecret, Spec, parse_spec
from datadouble.storage import filesystem_store
from fake_engine import FakeEngine, FakeRuntime, check_protocols
from fakes import shop

KEYLESS = "signing: {cosign: {keyless: true}}\n"


@dataclass
class FakeCi:
    """A CI system that records what it is told."""

    hosted: bool = False
    masked: list[str] = field(default_factory=list[str])
    reports: list[tuple[Report, Path | None]] = field(
        default_factory=list[tuple[Report, Path | None]]
    )
    pipelines: list[Pipeline] = field(default_factory=list[Pipeline])

    @property
    def name(self) -> str:
        return "fake-ci"

    def generate(self, pipeline: Pipeline) -> tuple[GeneratedFile, ...]:
        self.pipelines.append(pipeline)
        return tuple(
            GeneratedFile(path=PurePosixPath(f"ci/{job.kind}.txt"), content=f"{job.kind}\n")
            for job in pipeline.jobs
        )

    def instructions(self, pipeline: Pipeline) -> tuple[str, ...]:
        return (f"schedule {pipeline.jobs[0].kind}",) if pipeline.side is Side.PRODUCTION else ()

    def vendor_hosted(self, environ: Mapping[str, str]) -> str | None:
        return "a fake hosted runner" if self.hosted else None

    def run_url(self, environ: Mapping[str, str]) -> str | None:
        return environ.get("FAKE_RUN_URL")

    def mask(self, value: str) -> None:
        self.masked.append(value)

    def report(self, report: Report, directory: Path | None) -> ExitCode:
        self.reports.append((report, directory))
        return ExitCode.SUCCESS


def check_ci(adapter: FakeCi) -> FakeCi:
    """Fail to type-check unless the fake is a CI adapter."""
    checked = Registry(engines={}, stores={}, ci={"fake-ci": lambda: adapter})
    assert checked.ci_system("fake-ci") is adapter
    return adapter


@pytest.fixture
def ci() -> FakeCi:
    return check_ci(FakeCi())


@pytest.fixture
def registry(ci: FakeCi, monkeypatch: pytest.MonkeyPatch) -> Registry:
    engine = FakeEngine(catalog=shop(), source=dataset())
    runtime = FakeRuntime()
    built = Registry(
        engines={"fake": lambda: check_protocols(engine)},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: runtime},
        ci={"fake-ci": lambda: ci},
    )
    monkeypatch.setattr(api, "installed", lambda: built)
    monkeypatch.setattr(cli, "installed", lambda: built)
    return built


def options(
    *kinds: JobKind,
    runner: str | None = "prod-db",
    work: Path | None = Path("/srv/work"),
    side: Side | None = None,
) -> PipelineOptions:
    return PipelineOptions(
        kinds=kinds,
        spec_path="datadouble/shop.yml",
        image="datadouble:test",
        version="1.2.3",
        owner="999:999",
        runner=runner,
        work=work,
        side=side,
    )


def production_spec(tmp_path: Path) -> Spec:
    return make_spec(tmp_path, "secrets: {anonymization_key: {env: SHOP_KEY}}\n")


def test_the_production_side(tmp_path: Path) -> None:
    spec = production_spec(tmp_path)
    planned = pipeline(spec, options(JobKind.PROMOTE, JobKind.BUILD, JobKind.PRUNE))
    assert planned.side is Side.PRODUCTION
    build, promote, prune = planned.jobs
    assert build.kind is JobKind.BUILD
    assert build.schedule == "0 2 * * 1"
    assert build.invocations[0].arguments == (
        "run",
        "plan,extract,load,transform,package,verify,store",
        "--spec",
        "datadouble/shop.yml",
        "--work",
        "/srv/work",
        "--retry-key",
        Placeholder.RUN,
    )
    assert build.mounts == tuple(sorted((str(tmp_path / "build"), "/srv/work")))
    assert build.environment == ("COSIGN_KEY", "COSIGN_PASSWORD", "SHOP_KEY")
    assert promote.after is JobKind.BUILD
    assert promote.triggers == (Trigger.AFTER, Trigger.MANUAL)
    assert promote.environment == ("COSIGN_PUB",)
    assert promote.mounts == (str(tmp_path / "build"), str(tmp_path / "export"))
    assert [i.arguments[4] for i in prune.invocations] == ["store", "export"]
    assert prune.parameters == (Placeholder.DRY_RUN,)
    assert prune.environment == ()
    assert planned.environment() == ("COSIGN_KEY", "COSIGN_PASSWORD", "COSIGN_PUB", "SHOP_KEY")
    assert not planned.identity_token
    keyless = make_spec(tmp_path).model_copy(update={"signing": parse_spec(SPEC + KEYLESS).signing})
    assert pipeline(keyless, options(JobKind.BUILD)).identity_token
    promote_only = pipeline(spec, options(JobKind.PROMOTE))
    assert promote_only.jobs[0].triggers == (Trigger.MANUAL,)


def test_pool_jobs(tmp_path: Path) -> None:
    pool = make_spec(tmp_path)
    kinds = (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.ACTIVATE, JobKind.PRUNE)
    planned = pipeline(pool, options(*kinds, runner=None))
    assert planned.side is Side.NON_PRODUCTION
    fetch, provision, release, prune, activate = planned.jobs
    assert fetch.schedule == "*/15 * * * *"
    assert fetch.environment == ("COSIGN_PUB",)
    assert provision.after is JobKind.FETCH
    assert provision.triggers == (Trigger.SCHEDULE, Trigger.MANUAL, Trigger.AFTER)
    assert provision.invocations[0].arguments == ("replicate", "--spec", "datadouble/shop.yml")
    assert release.invocations[0].arguments[-1] == "--expired"
    assert [i.arguments[4] for i in prune.invocations] == ["staging"]
    assert activate.parameters == (Placeholder.GENERATION,)
    assert activate.invocations[0].arguments[1] is Placeholder.GENERATION
    alone = pipeline(pool, options(JobKind.PROVISION, runner=None))
    assert alone.jobs[0].after is None
    assert alone.jobs[0].triggers == (Trigger.SCHEDULE, Trigger.MANUAL)


def test_on_demand_jobs(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, mode="on-demand")
    provision, release = pipeline(spec, options(JobKind.PROVISION, JobKind.RELEASE)).jobs
    assert provision.triggers == (Trigger.REQUEST_OPENED,)
    assert provision.invocations[0].arguments[-1] is Placeholder.OWNER
    closed, sweep = release.invocations
    assert closed.when == (Trigger.REQUEST_CLOSED,)
    assert closed.arguments[-1] is Placeholder.OWNER
    assert sweep.when == (Trigger.SCHEDULE, Trigger.MANUAL)


@pytest.mark.parametrize(
    ("kinds", "side", "problem"),
    [
        ((JobKind.BUILD, JobKind.FETCH), None, "generate each side separately"),
        ((JobKind.PRUNE,), None, "name the side"),
        ((JobKind.FETCH,), Side.PRODUCTION, "non-production side, not production"),
    ],
)
def test_sides(kinds: tuple[JobKind, ...], side: Side | None, problem: str) -> None:
    with pytest.raises(CiError, match=problem):
        side_of(kinds, side)


def test_the_lock_job(tmp_path: Path) -> None:
    spec = parse_spec(
        SPEC.replace("STORE", str(tmp_path / "store")).replace(
            "connection: {where: here}", "connection: {dsn: {env: SHOP_SOURCE_DSN}}"
        )
        + "secrets: {anonymization_key: {env: SHOP_KEY}}\n"
    )
    planned = pipeline(spec, options(JobKind.BUILD, JobKind.LOCK))
    assert planned.side is Side.PRODUCTION
    build, lock = planned.jobs
    assert lock.kind is JobKind.LOCK
    assert lock.triggers == (Trigger.MANUAL,)
    assert lock.schedule is None
    assert lock.invocations[0].arguments == (
        "lock", "--spec", "datadouble/shop.yml", "--output", "datadouble-lock"
    )  # fmt: skip
    assert lock.artifacts == "datadouble-lock"
    assert lock.environment == ("SHOP_SOURCE_DSN",)
    assert lock.mounts == ()
    assert "SHOP_KEY" in build.environment
    assert build.artifacts is None
    with pytest.raises(CiError, match="build, promote and lock run only inside"):
        pipeline(spec, options(JobKind.LOCK, runner=None))
    with pytest.raises(CiError, match="generate each side separately"):
        side_of((JobKind.LOCK, JobKind.FETCH), None)


def test_prune_alone_takes_a_side(tmp_path: Path) -> None:
    planned = pipeline(make_spec(tmp_path), options(JobKind.PRUNE, side=Side.NON_PRODUCTION))
    assert planned.side is Side.NON_PRODUCTION


def test_what_jobs_need(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    with pytest.raises(CiError, match="name the runner"):
        pipeline(spec, options(JobKind.PROMOTE, runner=None))
    for work in (None, Path("work")):
        with pytest.raises(CiError, match="build needs a work directory"):
            pipeline(spec, options(JobKind.BUILD, work=work))
    for kind, section in (
        (JobKind.PROMOTE, "promote"),
        (JobKind.FETCH, "fetch"),
        (JobKind.PROVISION, "replicate"),
    ):
        without = spec.model_copy(update={section: None})
        with pytest.raises(CiError, match=f"the spec's `{section}` section"):
            pipeline(without, options(kind))


def test_host_paths_and_secret_names() -> None:
    spec = parse_spec(
        """
name: shop
source:
  engine: fake
  connection:
    host: /var/run/postgresql
    password: {env: PGPASSWORD}
    url: "s3://x/y"
    note: "/* not a path */"
store: [{filesystem: {path: /srv/store/}}]
files:
  connection: {root: /srv/store/uploads, accept_inconsistent: true}
  select: {avatars: -mtime -30}
"""
    )
    assert host_paths(spec, JobKind.BUILD, Side.PRODUCTION, [Path("/srv/work")]) == (
        "/srv/store",
        "/srv/work",
        "/var/run/postgresql",
    )
    assert secret_names(spec, JobKind.BUILD, Side.PRODUCTION) == ("PGPASSWORD",)
    assert secret_names(spec, JobKind.FETCH, Side.NON_PRODUCTION) == ()


def test_build_jobs_get_account_passwords() -> None:
    spec = parse_spec(
        """
name: shop
source: {engine: fake}
package:
  accounts:
    - {user: app, password: {env: APP_PASSWORD}}
    - {user: reports, password: {file: /run/secrets/reports}, privileges: read}
    - {user: plain, password: written-in-the-spec}
    - {user: /srv/pathlike, password: /Tr0ub4dor}
"""
    )
    assert secret_names(spec, JobKind.BUILD, Side.PRODUCTION) == ("APP_PASSWORD",)
    # A password or user that looks like a path is not one.
    assert host_paths(spec, JobKind.BUILD, Side.PRODUCTION) == ("/run/secrets/reports",)
    assert secret_names(spec, JobKind.PROVISION, Side.NON_PRODUCTION) == ()


def test_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    masked: list[str] = []
    monkeypatch.setenv("SHOP_SECRET", "s3cret")
    mask("ignored")
    with masking(masked.append):
        assert resolve_secret(EnvSecret(env="SHOP_SECRET")) == "s3cret"
    assert resolve_secret(EnvSecret(env="SHOP_SECRET")) == "s3cret"
    assert masked == ["s3cret"]


def test_hosted_runners_are_refused(ci: FakeCi, registry: Registry) -> None:
    require_inside("reading the source", registry)
    ci.hosted = True
    with pytest.raises(BoundaryError, match="this is a fake hosted runner"):
        require_inside("reading the source", registry)


def write_spec(tmp_path: Path, spec: Spec) -> Path:
    path = tmp_path / "repo" / "datadouble" / "shop.yml"
    path.parent.mkdir(parents=True)
    path.write_text(spec.model_dump_json(by_alias=True, exclude_none=True), encoding="utf-8")
    return path


def run(*argv: str, stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out)
    return code, out.getvalue()


@pytest.mark.usefixtures("registry")
def test_ci_init(tmp_path: Path, ci: FakeCi) -> None:
    spec = write_spec(tmp_path, production_spec(tmp_path))
    repo = tmp_path / "repo"
    code, out = run(
        "ci",
        "init",
        "--system",
        "fake-ci",
        "--job",
        "build,promote",
        "--job",
        "build",
        "--spec",
        str(spec),
        "--runner",
        "prod-db",
        "--work",
        "/srv/work",
        "--output",
        str(repo),
        "--env",
        "AWS_ACCESS_KEY_ID",
    )
    assert code == ExitCode.SUCCESS, out
    assert out == (
        "wrote fake-ci configuration for the production side:\n"
        f"  {repo / 'ci/build.txt'}\n"
        f"  {repo / 'ci/promote.txt'}\n"
        "define these as secrets where the jobs run:\n"
        "  AWS_ACCESS_KEY_ID\n  COSIGN_KEY\n  COSIGN_PASSWORD\n  COSIGN_PUB\n  SHOP_KEY\n"
        "then, in the CI system:\n  schedule build\n"
    )
    assert (repo / "ci" / "build.txt").read_text() == "build\n"
    (planned,) = ci.pipelines
    assert planned.spec == "datadouble/shop.yml"
    assert planned.user == identity()
    assert planned.image == f"ghcr.io/datadouble/datadouble:v{__version__}"
    assert planned.version == __version__
    assert all("AWS_ACCESS_KEY_ID" in job.environment for job in planned.jobs)


@pytest.mark.usefixtures("registry")
def test_ci_init_without_secrets(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, make_spec(tmp_path))
    repo = tmp_path / "repo"
    code, out = run(
        "ci", "init", "--system", "fake-ci", "--job", "prune", "--side", "non-production",
        "--spec", str(spec), "--output", str(repo),
    )  # fmt: skip
    assert code == ExitCode.SUCCESS, out
    assert out == (
        f"wrote fake-ci configuration for the non-production side:\n  {repo / 'ci/prune.txt'}\n"
    )


@pytest.mark.usefixtures("registry")
def test_ci_init_problems(tmp_path: Path) -> None:
    spec = write_spec(tmp_path, make_spec(tmp_path, mode="on-demand"))
    outside = tmp_path / "elsewhere"
    request = {
        "command": "ci.init",
        "system": "fake-ci",
        "jobs": ["fetch"],
        "spec": str(spec),
        "output": str(outside),
    }
    code, out = run("api", stdin=json.dumps(request))
    response = json.loads(out)
    assert code == ExitCode.FAILURE
    assert response["error"] == ErrorCode.CI_FAILED
    assert "is not inside" in response["message"]
    request = {**request, "system": "jenkins", "output": str(tmp_path / "repo")}
    code, out = run("api", stdin=json.dumps(request))
    assert json.loads(out)["error"] == ErrorCode.ADAPTER_MISSING
    assert "install datadouble-jenkins" in json.loads(out)["message"]
    code, out = run(
        "--dry-run", "ci", "init", "--system", "x", "--job", "prune", "--spec", "s.yml",
        "--side", "production", "--image", "my/image:1",
    )  # fmt: skip
    assert json.loads(out) == {
        "command": "ci.init",
        "system": "x",
        "jobs": ["prune"],
        "spec": "s.yml",
        "side": "production",
        "image": "my/image:1",
    }


def test_jobs_must_be_known() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ci", "init", "--system", "x", "--job", "deploy"])


def spec_with_secret(tmp_path: Path) -> Path:
    path = tmp_path / "shop.yml"
    text = SPEC.replace("STORE", str(tmp_path / "store")).replace(
        "name: shop\n", "name: shop\nsecrets: {anonymization_key: {env: SHOP_KEY}}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.usefixtures("registry")
def test_bridged_requests(tmp_path: Path, ci: FakeCi, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = spec_with_secret(tmp_path)
    monkeypatch.setenv("SHOP_KEY", "key-material")
    code, out = run(
        "--ci", "fake-ci", "--ci-directory", str(tmp_path / "bridge"),
        "run", "plan,extract", "--spec", str(spec), "--no-strict",
        "--work", str(tmp_path / "work"), "--retry-key", "run-1",
    )  # fmt: skip
    assert code == ExitCode.SUCCESS, out
    ((report, directory),) = ci.reports
    assert directory == tmp_path / "bridge"
    assert report.command == "run"
    assert report.outcome is Outcome.WARNING
    assert report.warnings == ("no catalog lock: strict mode cannot compare anything",)
    assert set(report.outputs) == {"outcome", "generation"}
    assert report.summary == out.rstrip("\n")
    assert ci.masked == []
    code, out = run(
        "--ci", "fake-ci", "run", "plan,extract,load,transform", "--spec", str(spec), "--no-strict",
        "--work", str(tmp_path / "work"), "--retry-key", "run-1",
    )  # fmt: skip
    assert code == ExitCode.SUCCESS, out
    assert ci.reports[1][0].outputs["generation"] == report.outputs["generation"]
    assert ci.masked == ["key-material"]
    assert ci.reports[1][1] is None


@pytest.mark.usefixtures("registry")
def test_bridged_failures(tmp_path: Path, ci: FakeCi) -> None:
    code, out = run("--ci", "fake-ci", "fetch", "--spec", str(tmp_path / "missing.yml"))
    assert code == ExitCode.SUCCESS
    ((report, _),) = ci.reports
    assert report.command == "error"
    assert report.outcome is Outcome.FAILURE
    assert report.outputs == {"outcome": "failure", "error": "invalid_spec"}
    assert report.message is not None
    assert report.message.startswith("cannot read")
    code, out = run("--ci", "jenkins", "version")
    assert code == ExitCode.FAILURE
    assert out.startswith("error: no CI/CD adapter named 'jenkins'")
    code, out = run("api", stdin='{"command": "version", "ci": {"system": "fake-ci"}}')
    assert json.loads(out)["command"] == "version"
    assert ci.reports[-1][0].warnings == ()
    code, out = run("api", stdin='{"command": "nope"}')
    assert code == ExitCode.FAILURE


@pytest.mark.usefixtures("registry")
def test_bridged_lock(tmp_path: Path, ci: FakeCi) -> None:
    spec = spec_with_secret(tmp_path)
    code, out = run("--ci", "fake-ci", "lock", "--spec", str(spec))
    assert code == ExitCode.SUCCESS, out
    (report, _) = ci.reports[-1]
    assert set(report.outputs) == {"outcome", "fingerprint"}
    assert report.files == ()
    review = tmp_path / "datadouble-lock"
    code, out = run("--ci", "fake-ci", "lock", "--spec", str(spec), "--output", str(review))
    assert code == ExitCode.SUCCESS, out
    (report, _) = ci.reports[-1]
    assert report.outputs["changes"] == "0"
    assert report.files == (review / "catalog.lock", review / "changes.txt")
    assert report.summary == out.rstrip("\n")


def test_reports_carry_warnings(tmp_path: Path, registry: Registry) -> None:
    request = {"command": "plan", "spec": str(spec_with_secret(tmp_path)), "strict": "off"}
    planned = api.handle_json(json.dumps(request), registry)
    assert isinstance(planned, api.PlanResponse)
    warned = planned.model_copy(
        update={
            "outcome": Outcome.WARNING,
            "plan": planned.plan.model_copy(update={"warnings": ("look at this",)}),
        }
    )
    report = api.ci_report(warned, "summary")
    assert report.command == "plan"
    assert report.warnings == ("look at this",)
    assert report.outputs == {"outcome": "warning"}


def test_retry_keys(tmp_path: Path, registry: Registry) -> None:
    spec = spec_with_secret(tmp_path)
    base = {
        "command": "run",
        "spec": str(spec),
        "stages": ["plan"],
        "strict": "off",
        "work": str(tmp_path / "work"),
    }
    first = api.handle_json(json.dumps({**base, "retry_key": "a"}), registry)
    again = api.handle_json(json.dumps({**base, "retry_key": "a"}), registry)
    other = api.handle_json(json.dumps({**base, "retry_key": "b"}), registry)
    assert isinstance(first, api.RunResponse)
    assert isinstance(again, api.RunResponse)
    assert isinstance(other, api.RunResponse)
    assert again.result.generation == first.result.generation
    assert other.result.generation != first.result.generation
    both = api.handle_json(json.dumps({**base, "retry_key": "a", "resume": "x"}), registry)
    assert isinstance(both, api.ErrorResponse)
    assert both.details[0].problem.endswith("give resume or retry_key, not both")


@pytest.mark.usefixtures("registry")
def test_a_failed_notification_changes_nothing(
    tmp_path: Path, ci: FakeCi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = spec_with_secret(tmp_path)
    text = spec.read_text(encoding="utf-8")
    hook = "notify: {when: [warning], to: [{webhook: {url: {env: ALERTS_WEBHOOK}}}]}\n"
    spec.write_text(text + hook, encoding="utf-8")
    monkeypatch.setenv("SHOP_KEY", "key-material")
    monkeypatch.setenv("ALERTS_WEBHOOK", "ftp://hooks.example.com/shop")
    monkeypatch.setenv("FAKE_RUN_URL", "https://ci.example.com/runs/7")
    arguments = (
        "run", "plan,extract", "--spec", str(spec), "--no-strict",
        "--work", str(tmp_path / "work"),
    )  # fmt: skip
    code, out = run("--dry-run", "--ci", "fake-ci", "--ci-job", "build", *arguments)
    assert json.loads(out)["ci"] == {"system": "fake-ci", "job": "build"}
    code, out = run("--ci", "fake-ci", "--ci-job", "build", *arguments)
    assert code == ExitCode.SUCCESS, out
    ((report, _),) = ci.reports
    assert report.outcome is Outcome.WARNING
    warning = capsys.readouterr().err
    assert warning == (
        "warning: a notification failed, webhook 1: its URL must start with https:// or"
        " http:// and name a host\n"
    )
    assert "hooks.example.com" not in warning
    assert "ftp://hooks.example.com/shop" in ci.masked
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--ci-job", "deploy", "version"])
