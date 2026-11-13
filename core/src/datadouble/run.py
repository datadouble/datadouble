# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Running the build stages, with manifests and resume.

A run belongs to a generation, and keeps its work under
``<work dir>/<spec>/<generation>/``. Every stage writes its artifact and then
a manifest recording a hash of its inputs. Resuming a generation skips each
stage whose recorded input hash still matches, so a killed run picks up where
it stopped and a changed anonymization rule re-runs from ``transform``.

The work directory holds real personal data from ``extract`` until
``transform`` has run: it is production scratch space and never leaves
production. Only verified images reach a store.
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, TypeAdapter

from datadouble import __version__
from datadouble.anonymize import Anonymizer, new_key
from datadouble.catalog import CatalogLock
from datadouble.engine import (
    EngineAdapter,
    EngineError,
    ExtractResult,
    Mapper,
    SourceSession,
    TableCount,
    Tooling,
    Transformation,
    WorkingDatabases,
)
from datadouble.images import (
    ImageArchive,
    ImageError,
    ImageIdentity,
    ImageSummary,
    Verification,
    new_generation,
    pack,
    unpack,
)
from datadouble.names import Generation, GroupName
from datadouble.planner import Plan, PlannedGroup, PlannedRule, PlanOptions, plan
from datadouble.registry import Registry
from datadouble.secrets import resolve_secret
from datadouble.signing import bundle_key, sign
from datadouble.spec import FakerRule, FormatPreservingRule, HashRule, Spec, StrictLevel
from datadouble.storage import Store
from datadouble.subset import key_conditions, nested_conditions
from datadouble.verify import FormatReport, VerificationReport, check_image, restore_failed


class Stage(StrEnum):
    """The build stages, in pipeline order."""

    PLAN = "plan"
    EXTRACT = "extract"
    LOAD = "load"
    TRANSFORM = "transform"
    PACKAGE = "package"
    VERIFY = "verify"
    STORE = "store"


class RunError(Exception):
    """A run that cannot continue."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class RunRecord(_Model):
    """What a generation was started with; read back when resuming."""

    generation: Generation
    spec: str
    created_at: AwareDatetime


class StageManifest(_Model):
    """A completed stage: what went in, how long it took and what came out."""

    stage: Stage
    group: GroupName | None
    inputs: str
    finished_at: AwareDatetime
    seconds: float
    outputs: dict[str, JsonValue]


class StageStatus(StrEnum):
    """Whether a stage did its work this time."""

    RAN = "ran"
    REUSED = "reused"


class StageRecord(_Model):
    """One stage's part in a run."""

    stage: Stage
    group: GroupName | None
    status: StageStatus
    seconds: float


class RunResult(_Model):
    """The outcome of a run, returned in the JSON response."""

    generation: Generation
    work: str
    stages: tuple[StageRecord, ...]
    verification: tuple[VerificationReport, ...]
    images: tuple[ImageSummary, ...]
    stored: tuple[str, ...]
    warnings: tuple[str, ...]

    def failed(self) -> bool:
        """Whether any verification failed."""
        return any(not report.passed() for report in self.verification)


class RunOptions(_Model):
    """How to run: which stages, where, and overrides over the spec."""

    stages: tuple[Stage, ...]
    work: Path
    resume: Generation | None = None
    plan: PlanOptions = PlanOptions()


_JSON = TypeAdapter(JsonValue)
_MANIFEST = "manifest.json"
_VALUE_RULES = (FakerRule, HashRule, FormatPreservingRule)


def fingerprint(value: object) -> str:
    """A SHA-256 over a value's canonical JSON form."""
    data = TypeAdapter(object).dump_python(value, mode="json")
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def value_mapper(anonymizer: Anonymizer) -> Mapper:
    """Replacements for value rules; other rules are applied by the engine."""

    def mapper(rule: PlannedRule, values: Iterable[str]) -> dict[str, str]:
        value_rule = rule.rule
        if not isinstance(value_rule, _VALUE_RULES):
            message = f"{rule.column}: {type(value_rule).__name__} is applied by the engine"
            raise RunError(message)
        unique = isinstance(value_rule, FakerRule) and value_rule.unique
        return anonymizer.mapping(value_rule, rule.kind, values, unique=unique)

    return mapper


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class Run:
    """One generation's work directory and the stages that fill it."""

    def __init__(
        self,
        spec: Spec,
        lock: CatalogLock | None,
        registry: Registry,
        options: RunOptions,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        """Open a new generation or the one being resumed."""
        self.spec = spec
        self.lock = lock
        self.registry = registry
        self.options = options
        self.clock = clock
        self.engine: EngineAdapter = registry.engine(spec.source.engine)
        self.record = self._record()
        self.root = (options.work / spec.name / self.record.generation).resolve()
        self.records: list[StageRecord] = []
        self.warnings: list[str] = []
        self._session: SourceSession | None = None
        self._tooling: Tooling | None = None
        self._exits = ExitStack()

    # The generation

    def _record(self) -> RunRecord:
        if self.options.resume is None:
            created = self.clock()
            generation = new_generation(int(created.timestamp() * 1000))
            record = RunRecord(generation=generation, spec=self.spec.name, created_at=created)
            root = self.options.work / self.spec.name / generation
            root.mkdir(parents=True, exist_ok=False)
            (root / "run.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
            return record
        path = self.options.work / self.spec.name / self.options.resume / "run.json"
        if not path.is_file():
            message = f"there is no generation {self.options.resume} of {self.spec.name} to resume"
            raise RunError(message)
        return RunRecord.model_validate_json(path.read_bytes())

    def key(self) -> bytes:
        """The anonymization key: from the spec, or generated once per generation."""
        ref = self.spec.secrets.anonymization_key
        if ref is not None:
            return resolve_secret(ref).encode()
        path = self.root / "key"
        if not path.is_file():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(new_key())
        return path.read_bytes()

    # Stage bookkeeping

    def _dir(self, stage: Stage, group: GroupName | None = None) -> Path:
        return self.root / stage if group is None else self.root / stage / group

    def manifest(self, stage: Stage, group: GroupName | None = None) -> StageManifest | None:
        """A completed stage's manifest, if there is one."""
        path = self._dir(stage, group) / _MANIFEST
        return StageManifest.model_validate_json(path.read_bytes()) if path.is_file() else None

    def required(self, stage: Stage, group: GroupName | None = None) -> StageManifest:
        """A completed stage's manifest; raises if the stage has not completed."""
        found = self.manifest(stage, group)
        if found is None:
            message = (
                f"stage {stage} has not completed in generation {self.record.generation}; "
                "include it in the stages to run"
            )
            raise RunError(message)
        return found

    def _discard(self, directory: Path) -> None:
        """Clear an earlier attempt's directory.

        It is moved aside first: a process the interrupted attempt started may
        still be writing into it, and must not get in the way of the new attempt.
        """
        if self._tooling is not None:
            self._tooling.runtime.release(directory)
        stale = self.root / ".stale" / f"{directory.name}-{time.time_ns()}"
        stale.parent.mkdir(exist_ok=True)
        directory.rename(stale)
        shutil.rmtree(stale, ignore_errors=True)

    def _wanted(self, stage: Stage) -> bool:
        return stage in self.options.stages

    def _stage(
        self,
        stage: Stage,
        group: GroupName | None,
        inputs: object,
        work: Callable[[Path], dict[str, JsonValue]],
    ) -> StageManifest:
        """Run ``work`` in a fresh directory unless a manifest with these inputs exists."""
        digest = fingerprint(inputs)
        existing = self.manifest(stage, group)
        if existing is not None and existing.inputs == digest:
            self.records.append(
                StageRecord(stage=stage, group=group, status=StageStatus.REUSED, seconds=0.0)
            )
            return existing
        if not self._wanted(stage):
            message = (
                f"stage {stage} has to run (its inputs changed or it never ran) but was "
                "not requested; include it in the stages to run"
            )
            raise RunError(message)
        directory = self._dir(stage, group)
        if directory.exists():
            self._discard(directory)
        directory.mkdir(parents=True)
        started = time.monotonic()
        outputs = work(directory)
        seconds = round(time.monotonic() - started, 3)
        manifest = StageManifest(
            stage=stage,
            group=group,
            inputs=digest,
            finished_at=self.clock(),
            seconds=seconds,
            outputs=outputs,
        )
        (directory / _MANIFEST).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        self.records.append(
            StageRecord(stage=stage, group=group, status=StageStatus.RAN, seconds=seconds)
        )
        return manifest

    # Stages

    def tooling(self) -> Tooling:
        """The container runtime engine software runs in, built on first use."""
        if self._tooling is None:
            if self.spec.runtime is None:
                message = (
                    "the spec has no `runtime`; extract and the stages after it run engine "
                    "software in containers (`runtime: {docker: {}}` or `{kubernetes: ...}`)"
                )
                raise RunError(message)
            ((kind, settings),) = self.spec.runtime.items()
            self._tooling = Tooling(
                runtime=self.registry.runtime(kind, settings),
                image=self.spec.source.image,
                server=self.spec.source.server,
            )
        return self._tooling

    def session(self) -> SourceSession:
        """The source connection, opened on first use."""
        if self._session is None:
            self._session = self._exits.enter_context(
                self.engine.connect(self.spec.source.connection)
            )
        return self._session

    def plan(self) -> Plan:
        """Plan against the live catalog, or reuse the generation's plan."""
        path = self._dir(Stage.PLAN) / "plan.json"
        if not self._wanted(Stage.PLAN):
            if not path.is_file() or self.manifest(Stage.PLAN) is None:
                message = f"generation {self.record.generation} has no plan; include plan"
                raise RunError(message)
            return Plan.model_validate_json(path.read_bytes())
        catalog = self.session().catalog()
        options = self.options.plan.model_copy(update={"now": self.record.created_at})
        planned = plan(self.spec, catalog, self.lock, self.engine.capabilities(), options)
        inputs = {
            "spec": self.spec,
            "catalog": catalog.fingerprint(),
            "lock": None if self.lock is None else self.lock.fingerprint,
            "options": options,
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            (directory / "plan.json").write_text(planned.model_dump_json(indent=2), "utf-8")
            return {"strategy": planned.strategy.value, "tables": len(planned.extracted())}

        self._stage(Stage.PLAN, None, inputs, work)
        self.warnings.extend(planned.warnings)
        return Plan.model_validate_json(path.read_bytes())

    def extract(self, planned: Plan) -> ExtractResult:
        """Read the chosen rows from one snapshot."""
        inputs = {
            "tables": planned.tables,
            "edges": planned.edges,
            "strategy": planned.strategy,
            "as_of": planned.as_of,
            "excludes": planned.excludes,
            "catalog": planned.catalog_fingerprint,
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            graph = planned.graph()
            with self.session().snapshot(planned.as_of) as snapshot:
                selections = (
                    nested_conditions(graph)
                    if planned.strategy == "nested"
                    else key_conditions(graph, snapshot)
                )
                result = snapshot.extract(planned, selections, directory / "data", self.tooling())
            return {"result": _JSON.validate_python(result.model_dump(mode="json"))}

        manifest = self._stage(Stage.EXTRACT, None, inputs, work)
        return _load(ExtractResult, manifest.outputs["result"])

    def load(self, working: WorkingDatabases, planned: Plan, group: PlannedGroup) -> None:
        """Build the group's working database."""
        inputs = {
            "extract": fingerprint(self.required(Stage.EXTRACT)),
            "group": group,
            "hooks": self.spec.hooks.before_load,
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            counts = working.load(
                planned,
                group,
                self._dir(Stage.EXTRACT) / "data",
                directory / "db",
                self.spec.hooks.before_load,
            )
            return {"counts": _counts(counts)}

        self._stage(Stage.LOAD, group.name, inputs, work)

    def transform(
        self, working: WorkingDatabases, planned: Plan, group: PlannedGroup, key: bytes
    ) -> None:
        """Anonymize a copy of the group's working database."""
        members = set(group.tables)
        rules = tuple(rule for rule in planned.anonymization if rule.column.table in members)
        anonymizer = Anonymizer(key)
        key_id = hmac.new(key, b"datadouble key id", hashlib.sha256).hexdigest()[:16]
        inputs = {
            "load": fingerprint(self.required(Stage.LOAD, group.name)),
            "rules": rules,
            "hooks": self.spec.hooks.after_transform,
            "key": key_id,
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            counts = working.transform(
                group,
                self._dir(Stage.LOAD, group.name) / "db",
                directory / "db",
                Transformation(rules, value_mapper(anonymizer), self.spec.hooks.after_transform),
            )
            return {
                "counts": _counts(counts),
                "rules": {str(r.column): fingerprint(r.rule) for r in rules},
                "rewrites": [str(c) for c in planned.rewrites if c.table in members],
            }

        self._stage(Stage.TRANSFORM, group.name, inputs, work)

    def identity(self, group: PlannedGroup, version: str) -> ImageIdentity:
        """The identity of the group's image in this generation."""
        return ImageIdentity(
            spec=self.spec.name,
            group=group.name,
            engine=self.engine.name,
            engine_version=version,
            generation=self.record.generation,
        )

    def package(
        self,
        working: WorkingDatabases,
        planned: Plan,
        group: PlannedGroup,
        extract: ExtractResult,
    ) -> None:
        """Package every requested format of the group's image."""
        version = working.server_version()
        identity = self.identity(group, version.split(".")[0])
        inputs = {
            "transform": fingerprint(self.required(Stage.TRANSFORM, group.name)),
            "formats": planned.formats,
            "version": version,
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            transformed = self.required(Stage.TRANSFORM, group.name)
            build = {
                "identity": identity,
                "datadouble": __version__,
                "source_server_version": extract.server_version,
                "working_server_version": version,
                "snapshot": extract.snapshot,
                "snapshot_seconds": extract.snapshot_seconds,
                "as_of": planned.as_of,
                "strategy": planned.strategy,
                "strict": planned.strict,
                "excludes": planned.excludes,
                "group": group,
                "transform": transformed.outputs,
                "stages": {r.stage.value: r.status.value for r in self.records},
            }
            manifest = json.dumps(
                TypeAdapter(object).dump_python(build, mode="json"), indent=2, sort_keys=True
            ).encode()
            archives: list[JsonValue] = []
            for image_format in planned.formats:
                files = directory / str(image_format)
                files.mkdir()
                working.package(self._dir(Stage.TRANSFORM, group.name) / "db", image_format, files)
                archive = directory / identity.archive_key(image_format).rsplit("/", 1)[1]
                found = pack(files, manifest, archive)
                shutil.rmtree(files)
                entry = ImageArchive(
                    format=image_format, key=identity.archive_key(image_format), digest=found
                )
                archives.append(_JSON.validate_python(entry.model_dump(mode="json")))
            return {
                "identity": _JSON.validate_python(identity.model_dump(mode="json")),
                "archives": archives,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            }

        self._stage(Stage.PACKAGE, group.name, inputs, work)

    def verify(
        self, working: WorkingDatabases, planned: Plan, group: PlannedGroup
    ) -> VerificationReport:
        """Restore every format and check it."""
        packaged = self.required(Stage.PACKAGE, group.name)
        transformed = self.required(Stage.TRANSFORM, group.name)
        inputs = {
            "package": fingerprint(packaged),
            "edges": planned.edges,
            "pii_allow": planned.pii_allow,
            "assertions": planned.assertions,
        }
        expected = _load(tuple[TableCount, ...], transformed.outputs["counts"])

        def work(directory: Path) -> dict[str, JsonValue]:
            reports: list[FormatReport] = []
            for archive in _load(tuple[ImageArchive, ...], packaged.outputs["archives"]):
                file = self._dir(Stage.PACKAGE, group.name) / archive.key.rsplit("/", 1)[1]
                scratch = directory / str(archive.format)
                try:
                    image, _ = unpack(file, scratch / "archive")
                    with working.restore(image, archive.format, scratch / "restored") as restored:
                        reports.append(
                            check_image(planned, group, archive.format, restored, expected)
                        )
                except (EngineError, ImageError, OSError) as error:
                    reports.append(restore_failed(archive.format, str(error)))
                shutil.rmtree(scratch, ignore_errors=True)
            report = VerificationReport(group=group.name, formats=tuple(reports))
            return {"report": _JSON.validate_python(report.model_dump(mode="json"))}

        manifest = self._stage(Stage.VERIFY, group.name, inputs, work)
        return _load(VerificationReport, manifest.outputs["report"])

    def summary(
        self, planned: Plan, group: PlannedGroup, report: VerificationReport
    ) -> ImageSummary:
        """The image summary for the group's image."""
        packaged = self.required(Stage.PACKAGE, group.name)
        checks = report.checks()
        return ImageSummary(
            identity=_load(ImageIdentity, packaged.outputs["identity"]),
            built_at=packaged.finished_at,
            archives=_load(tuple[ImageArchive, ...], packaged.outputs["archives"]),
            manifest_sha256=str(packaged.outputs["manifest_sha256"]),
            strict=StrictLevel.OFF if planned.strict is None else planned.strict.level,
            verification=Verification(
                passed=report.passed(),
                checks=len(checks),
                failed=sum(not check.passed for check in checks),
            ),
        )

    def stores(self) -> list[Store]:
        """The spec's stores."""
        return [
            self.registry.store(kind, settings)
            for entry in self.spec.store
            for kind, settings in entry.items()
        ]

    def store(
        self, group: PlannedGroup, summary: ImageSummary, report: VerificationReport
    ) -> list[str]:
        """Write a verified image, its summary and its verification report to every store."""
        stores = self.stores()
        if not stores:
            message = "the spec lists no stores"
            raise RunError(message)
        inputs = {
            "verify": fingerprint(self.required(Stage.VERIFY, group.name)),
            "stores": self.spec.store,
        }

        def work(_directory: Path) -> dict[str, JsonValue]:
            written: list[JsonValue] = []
            signed, bundle = summary, None
            if self.spec.signing is not None:
                bundle = sign(summary, self.spec.signing.cosign)
                signed = summary.model_copy(update={"signature": bundle_key(summary)})
            for store in stores:
                for archive in summary.archives:
                    name = archive.key.rsplit("/", 1)[1]
                    store.put_file(archive.key, self._dir(Stage.PACKAGE, group.name) / name)
                report_key = summary.identity.summary_key().replace(".summary.json", ".verify.json")
                store.put_bytes(report_key, report.model_dump_json(indent=2).encode())
                if bundle is not None:
                    store.put_bytes(bundle_key(summary), bundle)
                # The summary goes last: its presence means the image is complete.
                store.put_bytes(
                    summary.identity.summary_key(), signed.model_dump_json(indent=2).encode()
                )
                written.append(f"{store.describe()}/{summary.identity.summary_key()}")
            return {"stored": written}

        manifest = self._stage(Stage.STORE, group.name, inputs, work)
        return _load(list[str], manifest.outputs["stored"])

    # The whole run

    def execute(self) -> RunResult:
        """Run the requested stages, reusing completed ones."""
        with self._exits:
            planned = self.plan()
            outputs = _Outputs()
            if self._needs(Stage.EXTRACT):
                extracted = self.extract(planned)
                working = self.engine.working(
                    self.root / "scratch", self.tooling(), extracted.server_version
                )
                context = _Context(
                    working=working,
                    planned=planned,
                    extract=extracted,
                    key=self.key() if self._needs(Stage.TRANSFORM) else b"",
                )
                for group in planned.groups:
                    self._group(context, group, outputs)
            return RunResult(
                generation=self.record.generation,
                work=str(self.root),
                stages=tuple(self.records),
                verification=tuple(outputs.reports),
                images=tuple(outputs.summaries),
                stored=tuple(outputs.stored),
                warnings=tuple(self.warnings),
            )

    def _needs(self, stage: Stage) -> bool:
        """Whether ``stage`` or a later one was requested."""
        order = list(Stage)
        return any(self._wanted(s) for s in order[order.index(stage) :])

    def _group(self, context: "_Context", group: PlannedGroup, outputs: "_Outputs") -> None:
        working, planned = context.working, context.planned
        if self._needs(Stage.LOAD):
            self.load(working, planned, group)
        if self._needs(Stage.TRANSFORM):
            self.transform(working, planned, group, context.key)
        if self._needs(Stage.PACKAGE):
            self.package(working, planned, group, context.extract)
        if self._needs(Stage.VERIFY):
            report = self.verify(working, planned, group)
            outputs.reports.append(report)
            summary = self.summary(planned, group, report)
            outputs.summaries.append(summary)
            # Only verified images are stored; a failed one makes the run fail.
            if self._wanted(Stage.STORE) and report.passed():
                outputs.stored.extend(self.store(group, summary, report))


@dataclass(frozen=True, slots=True)
class _Context:
    working: WorkingDatabases
    planned: Plan
    extract: ExtractResult
    key: bytes


@dataclass(slots=True)
class _Outputs:
    reports: list[VerificationReport] = field(default_factory=list[VerificationReport])
    summaries: list[ImageSummary] = field(default_factory=list[ImageSummary])
    stored: list[str] = field(default_factory=list[str])


def _counts(counts: tuple[TableCount, ...]) -> list[JsonValue]:
    return [_JSON.validate_python(count.model_dump(mode="json")) for count in counts]


def _load[T](kind: type[T], value: JsonValue) -> T:
    """Read a manifest field back into its model, through JSON so enums parse."""
    return TypeAdapter(kind).validate_json(json.dumps(value))
