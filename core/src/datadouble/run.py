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
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, TypeAdapter

from datadouble import __version__
from datadouble.anonymize import Anonymizer, new_key
from datadouble.boundary import require_inside
from datadouble.catalog import CatalogLock
from datadouble.engine import (
    EngineAdapter,
    EngineError,
    ExtractResult,
    Mapper,
    RequirementError,
    Snapshot,
    SourceSession,
    TableCount,
    Tooling,
    Transformation,
    WorkingDatabases,
    check_owner,
    unmet,
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
from datadouble.names import EngineName, Generation, GroupName, TableName
from datadouble.planner import Plan, PlannedGroup, PlannedPaths, PlannedRule, PlanOptions, plan
from datadouble.registry import Registry
from datadouble.secrets import resolve_secret
from datadouble.signing import bundle_key, sign
from datadouble.spec import FakerRule, FormatPreservingRule, HashRule, Spec, StrictLevel
from datadouble.storage import Store
from datadouble.subset import (
    AnyOf,
    Condition,
    Graph,
    ValuesIn,
    key_conditions,
    nested_conditions,
)
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
    retry_key: str | None = None
    """The CI system's ID for the run that started the generation (``RunOptions.retry_key``)."""


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
    engine: EngineName | None = None
    """The engine of a second source, such as ``files``; None for the spec's source."""


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
    retry_key: str | None = None
    """The CI system's ID for this run, the same when it is retried: a retry resumes
    the generation started under the same key, and anything else starts a new one."""
    plan: PlanOptions = PlanOptions()
    within: Path | None = None
    """The generation directory of the run whose spec's second source, such as its
    ``files``, this run builds."""


_JSON = TypeAdapter(JsonValue)
_MANIFEST = "manifest.json"
_PATHS = "paths.json"
"""The extract's file of the values of ``files.paths_from`` for the chosen rows."""
_PATH_LIST: TypeAdapter[list[str]] = TypeAdapter(list[str])
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
        """Open a new generation or the one being resumed, or join one (``options.within``)."""
        self.spec = spec
        self.lock = lock
        self.registry = registry
        self.options = options
        self.clock = clock
        self.engine: EngineAdapter = registry.engine(spec.source.engine)
        self.resumed = False
        if options.within is None:
            self.record = self._record()
            self.root = (options.work / spec.name / self.record.generation).resolve()
        else:
            self.record = RunRecord.model_validate_json((options.within / "run.json").read_bytes())
            self.root = options.within / spec.source.engine
            self.root.mkdir(exist_ok=True)
        self.records: list[StageRecord] = []
        self.warnings: list[str] = []
        self._session: SourceSession | None = None
        self._tooling: Tooling | None = None
        self._exits = ExitStack()

    # The generation

    def _retried(self) -> Generation | None:
        """The generation an earlier attempt with this run's retry key started."""
        key = self.options.retry_key
        if key is None:
            return None
        for path in sorted((self.options.work / self.spec.name).glob("*/run.json"), reverse=True):
            record = RunRecord.model_validate_json(path.read_bytes())
            if record.retry_key == key:
                return record.generation
        return None

    def _record(self) -> RunRecord:
        resume = self.options.resume or self._retried()
        if resume is None:
            created = self.clock()
            generation = new_generation(int(created.timestamp() * 1000))
            record = RunRecord(
                generation=generation,
                spec=self.spec.name,
                created_at=created,
                retry_key=self.options.retry_key,
            )
            root = self.options.work / self.spec.name / generation
            try:
                root.mkdir(parents=True, exist_ok=False)
                (root / "run.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
            except OSError as error:
                message = f"cannot start a generation in {self.options.work}: {error.strerror}"
                raise RunError(message) from None
            return record
        path = self.options.work / self.spec.name / resume / "run.json"
        if not path.is_file():
            message = f"there is no generation {resume} of {self.spec.name} to resume"
            raise RunError(message)
        self.resumed = True
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
                StageRecord(
                    stage=stage,
                    group=group,
                    status=StageStatus.REUSED,
                    seconds=0.0,
                    engine=self._second(),
                )
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
            StageRecord(
                stage=stage,
                group=group,
                status=StageStatus.RAN,
                seconds=seconds,
                engine=self._second(),
            )
        )
        return manifest

    def _second(self) -> EngineName | None:
        """The engine, if this run builds a second source."""
        return None if self.options.within is None else self.engine.name

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
            require_inside("reading the source", self.registry)
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
            "followed": None
            if self.options.within is None
            else fingerprint((self.options.within / Stage.EXTRACT / _MANIFEST).read_bytes()),
        }

        def work(directory: Path) -> dict[str, JsonValue]:
            graph = planned.graph()
            with self.session().snapshot(planned.as_of) as snapshot:
                selections = (
                    nested_conditions(graph)
                    if planned.strategy == "nested"
                    else key_conditions(graph, snapshot)
                )
                selections = self._followed(planned, selections)
                result = snapshot.extract(planned, selections, directory / "data", self.tooling())
                unmatched = 0
                if planned.paths_from is not None:
                    paths, unmatched = self._paths(
                        planned, planned.paths_from, graph, snapshot, selections
                    )
                    (directory / _PATHS).write_text(json.dumps(paths), "utf-8")
            return {
                "result": _JSON.validate_python(result.model_dump(mode="json")),
                "unmatched_paths": unmatched,
            }

        manifest = self._stage(Stage.EXTRACT, None, inputs, work)
        unmatched = manifest.outputs.get("unmatched_paths")
        if unmatched:
            self.warnings.append(
                f"files.paths_from: {unmatched} value(s) did not match the pattern, so their"
                " files were not copied; check `match`"
            )
        return _load(ExtractResult, manifest.outputs["result"])

    @staticmethod
    def _paths(
        planned: Plan,
        wanted: PlannedPaths,
        graph: Graph,
        snapshot: Snapshot,
        selections: Mapping[TableName, Condition],
    ) -> tuple[list[str], int]:
        """The paths the chosen rows name, and how many values the pattern did not match.

        The rows are read through the snapshot.
        """
        # A nested condition refers to other tables' selections, which a projection
        # cannot see; the keys strategy's conditions stand alone.
        keyed = key_conditions(graph, snapshot) if planned.strategy == "nested" else selections
        rows = snapshot.project(wanted.table, keyed[wanted.table], wanted.columns)
        paths: set[str] = set()
        unmatched = 0
        for row in rows:
            path = wanted.path(row)
            if path is not None:
                paths.add(path)
            elif all(value is not None for value in row):
                unmatched += 1
        return sorted(paths), unmatched

    def _followed(
        self, planned: Plan, selections: dict[TableName, Condition]
    ) -> dict[TableName, Condition]:
        """A second source's selections, with the rows the parent's ``paths_from`` names.

        Each of the second source's tables also gets the rows whose key is one
        of the paths; the adapter keeps those that are its own.
        """
        if self.options.within is None:
            return selections
        file = self.options.within / Stage.EXTRACT / _PATHS
        if not file.is_file():
            return selections
        paths = frozenset((path,) for path in _PATH_LIST.validate_json(file.read_bytes()))
        followed = dict(selections)
        # The plan's catalog holds exactly the extracted tables.
        for table in planned.catalog.tables:
            followed[table.name] = AnyOf(
                (selections[table.name], ValuesIn(table.primary_key, paths))
            )
        return followed

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
                "requirements": planned.catalog.requirements,
                "omitted": planned.omitted,
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
        """Run the requested stages, reusing completed ones.

        A spec with ``files`` builds them as a second source in the same
        generation: their extract follows the source's, and their groups' images
        come after the source's.
        """
        with self._exits:
            files = self._files()
            if files is not None:
                self._exits.enter_context(files.exits)
            planned = self.plan()
            files_planned = files.plan() if files is not None else None
            outputs = _Outputs()
            if self._needs(Stage.EXTRACT):
                with ExitStack() as opened:
                    builds: list[tuple[Run, _Context]] = [(self, self.extracted(planned))]
                    opened.callback(builds[0][1].working.close)
                    if files is not None and files_planned is not None:
                        builds.append((files, files.extracted(files_planned)))
                        opened.callback(builds[1][1].working.close)
                    for run, context in builds:
                        for group in context.planned.groups:
                            run.build(context, group, outputs)
                if self.resumed:
                    # The interrupted attempt may have started a server after this one
                    # released its stage; every server of this run has stopped by now.
                    self.tooling().runtime.release(self.root)
            records = [*self.records, *(files.records if files is not None else [])]
            warnings = [*self.warnings, *(files.warnings if files is not None else [])]
            return RunResult(
                generation=self.record.generation,
                work=str(self.root),
                stages=tuple(records),
                verification=tuple(outputs.reports),
                images=tuple(outputs.summaries),
                stored=tuple(outputs.stored),
                warnings=tuple(warnings),
            )

    def _files(self) -> "Run | None":
        """The run of the spec's ``files``, if it has them."""
        spec = self.spec.files_spec()
        if spec is None:
            return None
        # The spec's strict mode and formats are its source's; the files' have their own.
        options = self.options.model_copy(update={"plan": PlanOptions(), "within": self.root})
        return Run(spec, None, self.registry, options, self.clock)

    @property
    def exits(self) -> ExitStack:
        """What closes this run's connections."""
        return self._exits

    def extracted(self, planned: Plan) -> "_Context":
        """Extract, and open the working databases the groups are built in."""
        check_owner(self.engine)
        scratch = self.root / "scratch"
        # Check the image before reading the snapshot, which can take hours.
        checked = bool(planned.catalog.requirements) and self._wanted(Stage.EXTRACT)
        if checked:
            version = self.session().server_version()
            checking = self.engine.working(scratch, self.tooling(), version)
            try:
                _require(checking, planned)
            finally:
                checking.close()
        extracted = self.extract(planned)
        working = self.engine.working(scratch, self.tooling(), extracted.server_version)
        if planned.catalog.requirements and not checked:
            _require(working, planned)
        return _Context(
            working=working,
            planned=planned,
            extract=extracted,
            key=self.key() if self._needs(Stage.TRANSFORM) else b"",
        )

    def _needs(self, stage: Stage) -> bool:
        """Whether ``stage`` or a later one was requested."""
        order = list(Stage)
        return any(self._wanted(s) for s in order[order.index(stage) :])

    def build(self, context: "_Context", group: PlannedGroup, outputs: "_Outputs") -> None:
        """Build, check and store one group's image."""
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


def _require(working: WorkingDatabases, planned: Plan) -> None:
    """Fail unless the working image provides every requirement the copies keep."""
    problems = unmet(planned.catalog.requirements, working.available())
    if problems:
        raise RequirementError(problems)
