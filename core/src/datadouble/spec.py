# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The spec: what to extract, how to anonymize it, and where images go.

Specs are written in YAML or JSON and parsed once, at the boundary, into the
frozen models below. :func:`spec_schema` publishes the same models as a JSON
Schema, so the two formats cannot disagree.
"""

import datetime as dt
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from datadouble.catalog import ObjectKind
from datadouble.names import (
    DEFAULT_GROUP,
    EngineName,
    GroupName,
    SourceName,
    SpecName,
    check_name,
)


class SpecError(Exception):
    """A spec that cannot be read or does not validate."""

    def __init__(self, message: str, details: tuple[tuple[str, str], ...] = ()) -> None:
        """Keep the per-field problems alongside the summary."""
        super().__init__(message)
        self.details = details


class _Model(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", use_attribute_docstrings=True
    )


class StrictLevel(StrEnum):
    """How strict mode treats tables and columns that differ from the catalog lock."""

    OFF = "off"
    """Warn about new tables and columns and changed columns."""
    TABLES = "tables"
    """Fail on new tables; warn about new and changed columns."""
    COLUMNS = "columns"
    """Fail on new tables, new columns and changed columns."""


class ObjectLevel(StrEnum):
    """How strict mode treats one kind of object that differs from the catalog lock."""

    OFF = "off"
    """Compare nothing, unless a difference breaks something."""
    WARN = "warn"
    """Name each new, changed or removed object."""
    FAIL = "fail"
    """Fail the plan on any difference."""


class StrictKind(StrEnum):
    """The kinds of thing strict mode has a setting for."""

    DATA = "data"
    FUNCTIONS = "functions"
    VIEWS = "views"
    TRIGGERS = "triggers"
    TYPES = "types"
    SEQUENCES = "sequences"
    EVENTS = "events"


_STRICT_ORDER: dict[StrictLevel | ObjectLevel, int] = {
    StrictLevel.OFF: 0,
    StrictLevel.TABLES: 1,
    StrictLevel.COLUMNS: 2,
    ObjectLevel.OFF: 0,
    ObjectLevel.WARN: 1,
    ObjectLevel.FAIL: 2,
}


def _yaml_off(value: object) -> object:
    # YAML 1.1 reads `off` as false.
    return "off" if value is False else value


class Strict(_Model):
    """Strict mode's level for each kind of thing in the catalog; kinds left out take defaults.

    Whatever the levels, a difference that breaks something fails the plan,
    and a removed table or column always fails.
    """

    data: StrictLevel = StrictLevel.COLUMNS
    """Tables, columns and requirements: ``off``, ``tables`` or ``columns``."""
    functions: ObjectLevel = ObjectLevel.WARN
    """Functions and procedures: ``off``, ``warn`` or ``fail``."""
    views: ObjectLevel = ObjectLevel.WARN
    """Views and materialized views."""
    triggers: ObjectLevel = ObjectLevel.WARN
    """Triggers."""
    types: ObjectLevel = ObjectLevel.WARN
    """User-defined types and domains."""
    sequences: ObjectLevel = ObjectLevel.WARN
    """Sequences."""
    events: ObjectLevel = ObjectLevel.WARN
    """Scheduled events, such as MySQL's."""

    @field_validator("*", mode="before")
    @classmethod
    def _off(cls, value: object) -> object:
        return _yaml_off(value)

    def level(self, kind: StrictKind) -> StrictLevel | ObjectLevel:
        """The level set for ``kind``."""
        level: StrictLevel | ObjectLevel = getattr(self, kind.value)
        return level

    def weaker(self, required: "Strict") -> tuple[StrictKind, ...]:
        """The kinds whose level here is less strict than ``required``'s."""
        return tuple(
            kind
            for kind in StrictKind
            if _STRICT_ORDER[self.level(kind)] < _STRICT_ORDER[required.level(kind)]
        )

    def failing(self) -> bool:
        """Whether any kind of object is set to ``fail``."""
        return any(self.level(kind) is ObjectLevel.FAIL for kind in StrictKind)

    @classmethod
    def lowest(cls) -> "Strict":
        """Every kind at ``off``: what a build without a catalog lock compared."""
        off = ObjectLevel.OFF
        return cls(
            data=StrictLevel.OFF,
            functions=off,
            views=off,
            triggers=off,
            types=off,
            sequences=off,
            events=off,
        )


class StrictOverrides(_Model):
    """Levels a request sets over the spec's ``strict``, for the kinds it names."""

    data: StrictLevel | None = None
    functions: ObjectLevel | None = None
    views: ObjectLevel | None = None
    triggers: ObjectLevel | None = None
    types: ObjectLevel | None = None
    sequences: ObjectLevel | None = None
    events: ObjectLevel | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _off(cls, value: object) -> object:
        return _yaml_off(value)

    def over(self, strict: Strict) -> Strict:
        """``strict`` with these levels in place of its own."""
        return strict.model_copy(update=self.model_dump(exclude_none=True))


def _strict_mapping(value: object) -> object:
    match value:
        case str() | False:
            message = (
                "`strict` is a mapping of levels per kind, such as `strict: {data: tables}`; "
                "kinds left out take their defaults"
            )
            raise ValueError(message)
        case _:
            return value


class Strategy(StrEnum):
    """How the subset is computed (see the subsetting design)."""

    AUTO = "auto"
    """Nested queries when the selection graph is acyclic, otherwise keys."""
    NESTED = "nested"
    """Strategy A: one nested query per table, pushed to the source."""
    KEYS = "keys"
    """Strategy B: collect keys, compute the closure, then fetch rows by key."""


class Unselected(StrEnum):
    """What happens to a kept table that no seed or followed reference selects."""

    FAIL = "fail"
    """Fail the plan, naming each such table (the default)."""
    NONE = "none"
    """Copy none of its rows, and warn."""
    ALL = "all"
    """Copy every row."""


class ImageFormat(StrEnum):
    """The forms a golden image can take."""

    PHYSICAL = "physical"
    LOGICAL = "logical"


class OnMissing(StrEnum):
    """What to do with a reference whose parent row is not followed."""

    NULL = "null"
    """Set the referencing columns to null."""
    STUB = "stub"
    """Insert a placeholder parent row."""
    DROP = "drop"
    """Leave the referencing row out."""


class FakerKind(StrEnum):
    """The kinds of value the faker strategy generates."""

    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    NAME = "name"
    EMAIL = "email"
    USER_NAME = "user_name"
    PHONE_NUMBER = "phone_number"
    STREET_ADDRESS = "street_address"
    CITY = "city"
    POSTCODE = "postcode"
    COMPANY = "company"
    IPV4 = "ipv4"
    URL = "url"
    TEXT = "text"
    DATE_OF_BIRTH = "date_of_birth"


# Secrets are referred to, never written into specs.


class EnvSecret(_Model):
    """A secret read from an environment variable."""

    env: str = Field(min_length=1)


class FileSecret(_Model):
    """A secret read from a file, such as a mounted Kubernetes Secret."""

    file: Path


type SecretRef = EnvSecret | FileSecret


class Source(_Model):
    """One server to extract from.

    ``connection`` is checked by the engine adapter, which knows its fields.
    """

    engine: EngineName
    """The engine adapter; every source of a spec has the same one."""
    connection: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Where the server is and what to read, checked by the engine adapter."""
    image: str | None = None
    """The vendor image working databases and engine tools run in; by default
    the engine adapter picks the official image of the newest source's version.
    Every source gives the same image, or none does."""
    server: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Settings for the engine's working servers, checked by the engine adapter.
    Every source gives the same settings, since one working server holds them all."""
    omit: tuple[str, ...] = ()
    """Requirements (extensions, plugins, modules) this source uses that copies
    leave out, by name. Anything that uses one must be excluded too, or the
    build fails."""


class Follow(_Model):
    """Child edges to follow, as ``parent -> child`` or ``parent -> child.column``."""

    down: tuple[str, ...] = ()


class EdgePolicy(_Model):
    """What to do with one reference: follow it to all parents, or a fallback."""

    follow: Literal["all"] | None = None
    on_missing: OnMissing | None = None

    @field_validator("on_missing", mode="before")
    @classmethod
    def _yaml_null(cls, value: object) -> object:
        # `on_missing: null` in YAML is a null value, not the string "null".
        # Defaults are not validated, so None here was written explicitly.
        return OnMissing.NULL.value if value is None else value

    @model_validator(mode="after")
    def _exactly_one(self) -> "EdgePolicy":
        if (self.follow is None) == (self.on_missing is None):
            message = "give exactly one of `follow: all` or `on_missing`"
            raise ValueError(message)
        return self


class VirtualForeignKey(_Model):
    """A reference the schema does not declare, optionally only for some rows."""

    when: str | None = None


class JailerSubset(_Model):
    """A subset designed in Jailer, read on every run (see :mod:`datadouble.jailer`)."""

    model: Path
    """The extraction model, a ``.jm`` file."""
    datamodel: Path
    """The data model folder it was designed against."""


class Subset(_Model):
    """Starting filters and the policy for deriving everything else."""

    as_of: AwareDatetime | None = None
    strategy: Strategy = Strategy.AUTO
    seeds: dict[str, str] = Field(default_factory=dict[str, str])
    """Starting conditions; keys are table names or globs, such as `"*": "TRUE"`."""
    follow: Follow = Follow()
    edges: dict[str, EdgePolicy] = Field(default_factory=dict[str, EdgePolicy])
    virtual_fks: dict[str, VirtualForeignKey] = Field(default_factory=dict[str, VirtualForeignKey])
    unselected: Unselected | None = None
    """Kept tables that no seed or followed reference selects: `fail` (the
    default), `none` or `all`."""
    jailer: JailerSubset | None = None
    """A Jailer subset instead of seeds, follow, edges, virtual_fks and unselected."""

    @model_validator(mode="after")
    def _one_source(self) -> "Subset":
        if self.jailer is not None and (
            self.seeds
            or self.follow.down
            or self.edges
            or self.virtual_fks
            or self.unselected is not None
        ):
            message = (
                "a subset from Jailer cannot also have seeds, follow, edges, virtual_fks "
                "or unselected; change the Jailer model, or import it "
                "(datadouble import-jailer) and edit that"
            )
            raise ValueError(message)
        return self


class FakerRule(_Model):
    """Replace with a deterministic fake value."""

    faker: FakerKind
    unique: bool = False


class HashOptions(_Model):
    """Options for the hash strategy."""

    length: int = Field(default=16, ge=8, le=64)


class HashRule(_Model):
    """Replace with a keyed hash, so equal inputs stay equal."""

    hash: HashOptions


class NullOptions(_Model):
    """The null strategy has no options."""


class NullRule(_Model):
    """Set to null."""

    null: NullOptions


class FormatPreservingOptions(_Model):
    """Options for the format-preserving strategy."""

    luhn: bool = False
    """Keep the Luhn check digit valid, for card numbers."""


class FormatPreservingRule(_Model):
    """Replace digits with digits and letters with letters, keeping the shape."""

    format_preserving: FormatPreservingOptions


class ExpressionRule(_Model):
    """Replace with an engine-native expression, optionally only where a condition holds."""

    expression: str = Field(min_length=1)
    where: str | None = None


type Rule = Annotated[
    FakerRule | HashRule | NullRule | FormatPreservingRule | ExpressionRule,
    Field(union_mode="left_to_right"),
]


class Hook(_Model):
    """A native script run before load (server settings, extensions)."""

    sql: str = Field(min_length=1)


class TransformHook(_Model):
    """A native script run after the column rules, declaring what it rewrites."""

    sql: str = Field(min_length=1)
    rewrites: tuple[str, ...] = ()


class Hooks(_Model):
    """Hooks around load and transform."""

    before_load: tuple[Hook, ...] = ()
    after_transform: tuple[TransformHook, ...] = ()


type ObjectRoot = Annotated[
    dict[ObjectKind, str],
    Field(
        min_length=1,
        max_length=1,
        description=(
            "An object code outside Datadouble expects, as its kind and its name matched "
            "from the right like a table's: `{function: public.uuid_generate_v4}`, with "
            "argument types as the catalog spells them where routines are overloaded "
            '(`{function: "public.total(integer)"}`), or `{trigger: orders.audit}` '
            "(a PostgreSQL trigger's name is `table.trigger`)."
        ),
    ),
]


class PulledRows(StrEnum):
    """Which rows of a table a standalone group pulls into its image."""

    REFERENCED = "referenced"
    """The rows the image's rows reference, and those referencing them where
    the spec follows references down (the default)."""
    ALL = "all"
    """Every row of the source table."""
    NONE = "none"
    """None: the table's structure only."""


class ImageGroup(_Model):
    """One image's share of the extract."""

    include: tuple[str, ...] = Field(min_length=1)
    """The tables in the image, as globs matched from the right."""
    requires: tuple[GroupName, ...] = ()
    """Groups whose images a copy of this one is provisioned with, for references into them."""
    standalone: bool = False
    """Whether the image must work on its own: what its tables and objects
    need from outside the group is pulled into it (package planning)."""
    objects: tuple[ObjectRoot, ...] = ()
    """Objects code outside Datadouble expects in this image, such as
    ``{function: public.uuid_generate_v4}``; they join the image's closure."""
    rows: dict[str, PulledRows] = Field(default_factory=dict[str, PulledRows])
    """Which rows a standalone group pulls in, overriding ``referenced``: keys
    are a table name or glob, or a reference ``child.column -> parent`` (which
    wins over a table key), and values ``referenced``, ``all`` or ``none``."""

    @model_validator(mode="after")
    def _alone(self) -> "ImageGroup":
        if self.standalone and self.requires:
            message = "a standalone group works on its own, so it cannot also have `requires`"
            raise ValueError(message)
        if self.rows and not self.standalone:
            message = "`rows` says what a standalone group pulls in; set `standalone: true`"
            raise ValueError(message)
        return self


class AccountPrivileges(StrEnum):
    """What an account copies carry may do."""

    ALL = "all"
    """Every privilege, with the grant option; a PostgreSQL superuser."""
    READ = "read"
    """``SELECT`` on the image's data, plus what the engine gives every user."""


class CopyAccount(_Model):
    """An account every copy of a physical image carries, for logins over TCP."""

    user: str = Field(min_length=1)
    password: Annotated[str, Field(min_length=1)] | SecretRef
    """Plain text, since copies hold no personal data, or a secret reference."""
    privileges: AccountPrivileges = AccountPrivileges.ALL
    """``all``: every privilege, with the grant option (a PostgreSQL superuser);
    ``read``: ``SELECT`` on the image's data, plus what the engine gives every user."""


class Package(_Model):
    """Which image formats to produce; all the engine supports when omitted."""

    formats: tuple[ImageFormat, ...] | None = Field(default=None, min_length=1)
    accounts: tuple[CopyAccount, ...] = ()
    """Accounts physical images carry for consumers that serve them with the
    engine's own image; none when omitted."""


class PiiScan(_Model):
    """Values the PII scan accepts on purpose, as ``LIKE`` patterns per column."""

    allow: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])


class Assertion(_Model):
    """A native query that must return no rows."""

    name: str = Field(min_length=1)
    sql: str = Field(min_length=1)


class Verify(_Model):
    """Verification settings beyond the checks that always run."""

    pii_scan: PiiScan = PiiScan()
    assertions: tuple[Assertion, ...] = ()


type AdapterSettings = dict[str, dict[str, JsonValue]]
"""One adapter, as a mapping with a single key naming it: ``{s3: {bucket: ...}}``."""


def _one_adapter(value: AdapterSettings) -> AdapterSettings:
    if len(value) != 1:
        message = "name exactly one adapter, as a mapping with a single key"
        raise ValueError(message)
    return value


class CosignSigning(_Model):
    """Signing with cosign: a key pair, or keyless through the CI system's identity."""

    key: SecretRef | None = None
    """The private key, in cosign's encrypted format; its password is COSIGN_PASSWORD."""
    keyless: bool = False
    transparency_log: bool = True
    """Record signatures in the transparency log; off only where it cannot be reached."""

    @model_validator(mode="after")
    def _one_way(self) -> "CosignSigning":
        if (self.key is None) != self.keyless:
            message = "give exactly one of `key` or `keyless: true`"
            raise ValueError(message)
        return self


class Signing(_Model):
    """How ``store`` signs images."""

    cosign: CosignSigning


class CosignTrust(_Model):
    """Which signatures ``promote`` and ``fetch`` accept."""

    public_key: SecretRef | None = None
    identity: str | None = None
    """For keyless signatures: the signer's certificate identity, matched exactly."""
    issuer: str | None = None
    """For keyless signatures: the OIDC issuer, matched exactly."""
    transparency_log: bool = True
    """Require a transparency log entry; turn off only for signatures made without one."""

    @model_validator(mode="after")
    def _one_way(self) -> "CosignTrust":
        keyless = self.identity is not None and self.issuer is not None
        partial = (self.identity is None) != (self.issuer is None)
        if partial or (self.public_key is None) != keyless:
            message = "give either `public_key`, or both `identity` and `issuer`"
            raise ValueError(message)
        return self


class Trust(_Model):
    """The signatures images must carry to cross into non-production."""

    cosign: CosignTrust


class Promote(_Model):
    """Where ``promote`` copies verified images, and what it requires of them."""

    to: tuple[AdapterSettings, ...] = Field(min_length=1)
    """The export stores, one adapter each, such as ``{filesystem: ...}`` or ``{s3: ...}``;
    a filesystem store's ``mode`` (``"0640"`` by default) decides who can read the files,
    such as a web server serving them. Each also gets an index of its images per group
    (``<spec>/<group>/index.json``), which stores that cannot list keys, like ``http``, are
    read through."""
    channel: str = "latest"
    strict: Strict = Strict()
    """The least strict level, per kind, an image may have been built with."""

    @field_validator("strict", mode="before")
    @classmethod
    def _strict(cls, value: object) -> object:
        return _strict_mapping(value)

    @field_validator("to")
    @classmethod
    def _stores(cls, value: tuple[AdapterSettings, ...]) -> tuple[AdapterSettings, ...]:
        return tuple(_one_adapter(entry) for entry in value)

    @field_validator("channel")
    @classmethod
    def _channel(cls, value: str) -> str:
        return check_name("channel", value)


class Fetch(_Model):
    """Where ``fetch`` reads images from and keeps them."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        validate_by_name=True,
        use_attribute_docstrings=True,
    )

    source: AdapterSettings = Field(alias="from")
    """The export store to read, one adapter: ``{filesystem: ...}``, ``{s3: ...}``, or
    ``{http: ...}``, a web server serving it, which is read-only and reads the index
    ``promote`` writes."""
    staging: Path
    channel: str = "latest"
    staging_channel: str = "active"
    format: ImageFormat = ImageFormat.PHYSICAL
    engine_version: str | None = None
    """Which compatibility line's images to fetch, such as ``8.4`` (not ``8.4.13``);
    the newest line when omitted."""
    keep: int = Field(default=3, ge=1)
    """How many generations staging keeps, for ``activate`` to roll back to."""

    @field_validator("source")
    @classmethod
    def _store(cls, value: AdapterSettings) -> AdapterSettings:
        return _one_adapter(value)

    @field_validator("channel", "staging_channel")
    @classmethod
    def _channels(cls, value: str) -> str:
        return check_name("channel", value)


class ReplicateMode(StrEnum):
    """How ``replicate`` decides how many copies to make."""

    POOL = "pool"
    """Keep a number of unclaimed copies of the active generation ready."""
    ON_DEMAND = "on-demand"
    """Make copies for a named requester."""


class Replicate(_Model):
    """How copies are made from staging."""

    using: AdapterSettings
    mode: ReplicateMode = ReplicateMode.POOL
    size: int = Field(default=2, ge=1)
    """Unclaimed copies to keep ready (pool), or copies per owner (on-demand)."""
    expires_hours: int = Field(default=24, ge=1)
    """How long an on-demand copy lives before ``release --expired`` removes it."""

    @field_validator("using")
    @classmethod
    def _adapter(cls, value: AdapterSettings) -> AdapterSettings:
        return _one_adapter(value)


class Retention(_Model):
    """What ``prune`` keeps."""

    keep: int = Field(default=3, ge=1)
    keep_days: int = Field(default=7, ge=0)
    keep_versions: int = Field(default=2, ge=1)


class Secrets(_Model):
    """Secrets the run needs, by reference."""

    anonymization_key: SecretRef | None = None
    """Seeds deterministic anonymization; a random key per run when omitted."""


class NotifyOn(StrEnum):
    """Outcomes of a generated CI job that notify; ``notify.when`` is ``[failure]`` by default."""

    FAILURE = "failure"
    WARNING = "warning"


class Webhook(_Model):
    """A URL Datadouble posts a JSON summary of the job's outcome to."""

    url: SecretRef
    """A secret reference, since a webhook's URL usually carries its credential."""


class WebhookTarget(_Model):
    """``{webhook: {url: {env: ALERTS_WEBHOOK}}}``."""

    webhook: Webhook


class Email(_Model):
    """Mail sent by the CI system's own mechanism, where it has one."""

    to: tuple[Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+$")], ...] = Field(min_length=1)
    """The addresses to mail."""


class EmailTarget(_Model):
    """``{email: {to: [data-team@example.com]}}``."""

    email: Email


type NotifyTarget = WebhookTarget | EmailTarget


class Notify(_Model):
    """Who hears about generated CI jobs that fail or warn (decision 125).

    It lives in the spec rather than in ``ci init``'s options, so generating
    the jobs again keeps it.
    """

    when: tuple[NotifyOn, ...] = Field(default=(NotifyOn.FAILURE,), min_length=1)
    """The outcomes that notify: ``failure``, ``warning`` or both; ``[failure]`` by default."""
    to: tuple[NotifyTarget, ...] = Field(min_length=1)
    """Where notifications go, one target each: ``{webhook: ...}`` or ``{email: ...}``."""

    def sends(self, outcome: str) -> bool:
        """Whether a job ending with ``outcome`` notifies."""
        return outcome in {str(kind) for kind in self.when}

    def webhooks(self) -> tuple[Webhook, ...]:
        """The webhook targets, in order."""
        return tuple(t.webhook for t in self.to if isinstance(t, WebhookTarget))

    def emails(self) -> tuple[str, ...]:
        """Every address the email targets name, in order, without repeats."""
        found = [a for t in self.to if isinstance(t, EmailTarget) for a in t.email.to]
        return tuple(dict.fromkeys(found))


class PathsFrom(_Model):
    """How the source's chosen rows name files: a template over a table's columns.

    ``path: "documents/{customer_id}/{file_name}"`` builds a path from two
    columns; ``match``, if given, is a regular expression applied to the
    result whose first group is the path, for values such as URLs.
    """

    table: str
    path: str = Field(min_length=1)
    match: str | None = None


FILES = EngineName("files")
"""The engine that copies a ``files`` section's tree."""
FILES_SOURCE = SourceName("files")
"""The name of the ``files`` section's build's one source."""


class Files(_Model):
    """A file tree the source's rows refer to, copied beside it (decision 97).

    The ``files`` engine adapter reads it: each top-level directory is a
    table, and ``select`` gives each one ``find`` tests. Every top-level
    directory must be selected or excluded.
    """

    connection: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Where the tree is and how to read it, checked by the ``files`` adapter."""
    select: dict[str, str] = Field(default_factory=dict[str, str])
    """Each top-level directory's ``find`` tests, such as ``-type f -mtime -30``."""
    exclude: tuple[str, ...] = ()
    """Top-level directories to leave out."""
    paths_from: str | PathsFrom | None = None
    """How the source's chosen rows name files to copy too, relative to the root:
    a ``table.column`` holding paths, or a template and pattern (:class:`PathsFrom`)."""
    cleanup: tuple[str, ...] = ()
    """Shell commands run in the copy's root after it is made, such as removing EXIF data."""
    image: str | None = None
    """The image the cleanup commands run in."""


class Spec(_Model):
    """A whole spec."""

    model_config = ConfigDict(title="Datadouble spec")

    name: SpecName
    """The spec's name: the first part of its images' identity."""
    sources: dict[SourceName, Source] = Field(min_length=1)
    """The servers to copy from, by name: one engine, each with its connection.
    Every source's databases go into one working server, so their names must
    not collide; each source is read under its own snapshot."""
    exclude: tuple[str, ...] = ()
    """Tables left out of every image, as globs matched from the right: ``audit.*``
    is every table in a namespace ``audit``, ``shop.*.*`` every table in
    PostgreSQL's database ``shop``."""
    structure_only: tuple[str, ...] = ()
    """Tables copied without their rows, as globs."""
    case_sensitive: bool = False
    """Whether table and column names match case-sensitively."""
    strict: Strict = Strict()
    """How differences from the catalog lock warn or fail, per kind: ``data`` (``off``,
    ``tables`` or ``columns``), and ``functions``, ``views``, ``triggers``, ``types``,
    ``sequences`` and ``events`` (``off``, ``warn`` or ``fail``)."""
    subset: Subset = Subset()
    """Which rows to copy: seeds, followed references and conditions."""
    anonymize: dict[str, dict[str, Rule]] = Field(default_factory=dict[str, dict[str, Rule]])
    """Rules per table and column: ``faker``, ``hash``, ``null``, ``format_preserving``
    or ``expression``."""
    hooks: Hooks = Hooks()
    """Commands run before load and after transform."""
    files: Files | None = None
    """File trees copied with the rows, as a second source."""
    images: dict[GroupName, ImageGroup] = Field(
        default_factory=lambda: {DEFAULT_GROUP: ImageGroup(include=("*",))}
    )
    """Named images built from one extract; one image of every table by default."""
    objects: tuple[ObjectRoot, ...] = ()
    """Objects code outside Datadouble expects in every image, such as
    ``{view: reporting.daily}``; they join each image's closure."""
    package: Package = Package()
    """The image formats to produce."""
    verify: Verify = Verify()
    """Checks every image must pass: PII scan, row counts, assertions."""
    runtime: AdapterSettings | None = None
    """Where engine software runs from extract on: ``{docker: {...}}`` or ``{kubernetes: ...}``,
    which reaches another cluster through ``kubeconfig``, a secret reference, and starts pods
    in Datadouble's own namespace unless ``namespace`` names one."""
    store: tuple[dict[str, dict[str, JsonValue]], ...] = ()
    """Where images are stored after the build, one adapter each: ``{filesystem: ...}``,
    ``{s3: ...}``. The filesystem store takes ``path``, a directory, and ``mode``, an octal
    string (``"0640"`` by default) every file it writes gets whatever the umask; directories
    it creates get ``mode`` plus the search bit wherever it has the read bit (``0750``).
    Modes that let anyone but the owner write, or the owner not read and write, are
    refused."""
    signing: Signing | None = None
    """How the build signs its images."""
    trust: Trust | None = None
    """Which signatures ``promote`` and ``fetch`` accept."""
    promote: Promote | None = None
    """The export stores ``promote`` copies verified images to."""
    fetch: Fetch | None = None
    """Where ``fetch`` reads promoted images, and the staging directory it fills."""
    replicate: Replicate | None = None
    """How copies are made: the adapter, pool or on-demand, and how many."""
    announce: tuple[AdapterSettings, ...] = ()
    """Where copies are announced to consumers, one adapter each: ``{dotenv: ...}``,
    ``{ci-outputs: {}}``, ``{vault: ...}`` or ``{k8s-secret: {}}``, which writes Secrets
    in Datadouble's own namespace unless ``namespace`` names one."""
    prune: Retention = Retention()
    """How many images and how much time ``prune`` keeps."""
    secrets: Secrets = Secrets()
    """Secrets the build needs, by reference."""
    notify: Notify | None = None
    """Who hears when a generated CI job fails or warns: webhooks and email."""

    @property
    def engine(self) -> EngineName:
        """The engine every source uses."""
        return next(iter(self.sources.values())).engine

    @property
    def working(self) -> Source:
        """The first source, whose ``image`` and ``server`` every source shares."""
        return next(iter(self.sources.values()))

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: dict[SourceName, Source]) -> dict[SourceName, Source]:
        for name in value:
            check_name("source name", name)
        first_name, first = next(iter(value.items()))
        for name, source in value.items():
            if source.engine != first.engine:
                message = (
                    f"every source must use one engine: source {first_name} is {first.engine}"
                    f" and source {name} is {source.engine}"
                )
                raise ValueError(message)
            for setting in ("image", "server"):
                if getattr(source, setting) != getattr(first, setting):
                    message = (
                        f"sources {first_name} and {name} give different `{setting}` settings;"
                        " one working server holds every source, so give each the same one"
                    )
                    raise ValueError(message)
        return value

    def files_spec(self) -> "Spec | None":
        """The spec of the ``files`` section's build, sharing this one's images and storage."""
        if self.files is None:
            return None
        files = self.files
        return Spec(
            name=self.name,
            sources={
                FILES_SOURCE: Source(engine=FILES, connection=files.connection, image=files.image)
            },
            exclude=files.exclude,
            case_sensitive=self.case_sensitive,
            # Every top-level directory must be selected or excluded instead.
            strict=Strict(data=StrictLevel.OFF),
            subset=Subset(as_of=self.subset.as_of, seeds=files.select),
            hooks=Hooks(after_transform=tuple(TransformHook(sql=line) for line in files.cleanup)),
            # File trees have no objects for a group to declare.
            images={
                name: group.model_copy(update={"objects": ()})
                for name, group in self.images.items()
            },
            # Accounts belong to the source's physical images, not the files'.
            package=self.package.model_copy(update={"accounts": ()}),
            runtime=self.runtime,
            store=self.store,
            signing=self.signing,
            secrets=self.secrets,
        )

    @field_validator("runtime")
    @classmethod
    def _runtime(cls, value: AdapterSettings | None) -> AdapterSettings | None:
        return None if value is None else _one_adapter(value)

    @field_validator("announce")
    @classmethod
    def _announcers(cls, value: tuple[AdapterSettings, ...]) -> tuple[AdapterSettings, ...]:
        return tuple(_one_adapter(entry) for entry in value)

    @field_validator("strict", mode="before")
    @classmethod
    def _strict(cls, value: object) -> object:
        return _strict_mapping(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: SpecName) -> SpecName:
        return SpecName(check_name("spec name", value))

    @field_validator("images")
    @classmethod
    def _groups(cls, value: dict[GroupName, ImageGroup]) -> dict[GroupName, ImageGroup]:
        if not value:
            message = "list at least one image group, or leave `images` out"
            raise ValueError(message)
        for name, group in value.items():
            check_name("image group", name)
            for required in group.requires:
                if required not in value:
                    message = f"group {name!r} requires {required!r}, which is not defined"
                    raise ValueError(message)
        return value

    @field_validator("store")
    @classmethod
    def _stores(
        cls, value: tuple[dict[str, dict[str, JsonValue]], ...]
    ) -> tuple[dict[str, dict[str, JsonValue]], ...]:
        for entry in value:
            if len(entry) != 1:
                message = "each store is a mapping with one key, the store kind"
                raise ValueError(message)
        return value


def _plain(value: object) -> object:
    # YAML turns unquoted timestamps into datetimes; the models parse ISO text.
    if isinstance(value, dt.date):
        return value.isoformat()
    message = f"unsupported value {value!r}"
    raise TypeError(message)


_RENAMED = (
    "`source` is now `sources`, a map of named sources: "
    "`sources: {app: {engine: ..., connection: ...}}`"
)


def parse_spec(text: str, *, origin: str = "spec") -> Spec:
    """Parse a spec from YAML or JSON text (JSON is valid YAML)."""
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as error:
        message = f"{origin} is not valid YAML or JSON: {error}"
        raise SpecError(message) from None
    try:
        text = json.dumps(raw, default=_plain)
    except TypeError as error:
        message = f"{origin} holds a value that has no JSON form: {error}"
        raise SpecError(message) from None
    try:
        return Spec.model_validate_json(text)
    except ValidationError as error:
        details = tuple(
            (
                ".".join(str(part) for part in problem["loc"]) or "(spec)",
                _RENAMED if problem["loc"] == ("source",) else problem["msg"],
            )
            for problem in error.errors(include_url=False)
        )
        message = f"{origin} does not match the spec schema: {error.error_count()} problem(s)"
        raise SpecError(message, details) from None


def load_spec(path: Path) -> Spec:
    """Read and parse a spec file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        message = f"cannot read {path}: {error.strerror}"
        raise SpecError(message) from None
    return parse_spec(text, origin=str(path))


def spec_schema() -> dict[str, JsonValue]:
    """The JSON Schema for specs."""
    schema: dict[str, JsonValue] = Spec.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema
