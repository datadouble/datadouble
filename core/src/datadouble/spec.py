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
    """How strict mode treats tables, columns and requirements that differ from the catalog lock.

    - ``off``: warn about new tables, new columns, changed columns and new
      requirements.
    - ``tables``: fail on new tables and new requirements, and warn about new
      and changed columns.
    - ``columns``: fail on new tables, new columns, changed columns and new
      requirements. This is the default.

    Requirements are the extensions, plugins and modules a source uses.
    """

    OFF = "off"
    TABLES = "tables"
    COLUMNS = "columns"


class ObjectLevel(StrEnum):
    """How strict mode treats one kind of object that differs from the catalog lock.

    - ``off``: compare nothing, unless a difference breaks something.
    - ``warn``: name each new, changed or removed object. This is the default.
    - ``fail``: fail the plan on any difference.
    """

    OFF = "off"
    WARN = "warn"
    FAIL = "fail"


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
    """Strict mode's level for each kind of catalog object.

    Strict mode compares the source's catalog with the catalog lock, and warns
    or fails on what differs. Each kind has its own level. Kinds left
    out take their defaults.

    Some differences fail the plan whatever the levels: a removed table or
    column, a table whose keys changed, a removed requirement or one at
    another version, and any difference that breaks something.
    """

    data: StrictLevel = StrictLevel.COLUMNS
    """Strict mode's level for tables, columns and requirements: ``off``,
    ``tables`` or ``columns``. Defaults to ``columns``."""
    functions: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for functions, procedures and packages of routines:
    ``off``, ``warn`` or ``fail``. Defaults to ``warn``."""
    views: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for views and materialized views: ``off``, ``warn``
    or ``fail``. Defaults to ``warn``."""
    triggers: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for triggers: ``off``, ``warn`` or ``fail``.
    Defaults to ``warn``."""
    types: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for user-defined types and domains: ``off``,
    ``warn`` or ``fail``. Defaults to ``warn``."""
    sequences: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for sequences: ``off``, ``warn`` or ``fail``.
    Defaults to ``warn``."""
    events: ObjectLevel = ObjectLevel.WARN
    """Strict mode's level for scheduled events, such as MySQL's: ``off``,
    ``warn`` or ``fail``. Defaults to ``warn``."""

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
    """How the planner computes the subset. The subsetting design explains both strategies.

    - ``auto``: nested queries when the selection graph has no cycle, the
      engine can run them, and no reference joins two sources. Keys
      otherwise. This is the default.
    - ``nested``: strategy A. One nested query per table, pushed down to the
      source.
    - ``keys``: strategy B. Collect keys, compute the closure of rows to copy,
      then fetch the rows by key.
    """

    AUTO = "auto"
    NESTED = "nested"
    KEYS = "keys"


class Unselected(StrEnum):
    """What happens to a kept table that no seed or followed reference selects.

    - ``fail``: fail the plan, naming each such table. This is the default.
    - ``none``: copy none of the table's rows, and warn.
    - ``all``: copy every row of the table.
    """

    FAIL = "fail"
    NONE = "none"
    ALL = "all"


class ImageFormat(StrEnum):
    """The golden image formats.

    - ``physical``: the engine's own on-disk form. It is the fastest to clone
      and start, but it is tied to the server version.
    - ``logical``: a portable export, which restores into other server
      versions.
    """

    PHYSICAL = "physical"
    LOGICAL = "logical"


class OnMissing(StrEnum):
    """What to do with a reference whose parent row is not followed.

    - ``null``: set the referencing columns to null.
    - ``stub``: insert a placeholder parent row.
    - ``drop``: leave the referencing row out.
    """

    NULL = "null"
    STUB = "stub"
    DROP = "drop"


class FakerKind(StrEnum):
    """The kinds of fake value the ``faker`` strategy generates."""

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
    """A secret read from an environment variable, written ``{env: NAME}``."""

    env: str = Field(min_length=1)
    """The environment variable that holds the secret. Must not be empty."""


class FileSecret(_Model):
    """A secret read from a file, such as a mounted Kubernetes Secret, written ``{file: PATH}``."""

    file: Path
    """The path of the file that holds the secret."""


type SecretRef = Annotated[
    EnvSecret | FileSecret,
    Field(
        description=(
            "A reference to a secret, so that the spec never holds the secret itself. "
            "`{env: NAME}` reads it from an environment variable, and `{file: PATH}` "
            "reads it from a file."
        )
    ),
]


class Source(_Model):
    """One server to copy from.

    Datadouble checks the fields below. The engine adapter checks the settings
    inside ``connection`` and ``server``, since only it knows their fields. Its
    README lists them.
    """

    engine: EngineName
    """The engine adapter that reads this source, such as ``postgres``. Every
    source of a spec must use the same one."""
    connection: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Where the server is and what to read. The engine adapter checks these
    settings."""
    image: str | None = None
    """The vendor image that working databases and engine tools run in. By
    default, the engine adapter picks the official image for the newest
    source's version. Either every source gives the same image, or none does."""
    server: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Settings for the engine's working servers, such as PostgreSQL's
    ``shared_preload_libraries``. The engine adapter checks them. Every source
    must give the same settings, because one working server holds every source."""
    omit: tuple[str, ...] = ()
    """Requirements this source uses that copies leave out, by name.
    Requirements are extensions, plugins and modules. Anything that uses an
    omitted requirement must be excluded too, or the build fails."""


class Follow(_Model):
    """Which references to follow down, from parent rows to their child rows.

    Parents of copied rows are always copied. Children are copied only along
    the edges listed here.
    """

    down: tuple[str, ...] = ()
    """Child edges to follow, each written ``parent -> child``. Write
    ``parent -> child.column`` to name the referencing column."""


class EdgePolicy(_Model):
    """What to do with one reference, keyed ``child.column -> parent`` in ``subset.edges``.

    Give exactly one of ``follow: all`` or ``on_missing``.
    """

    follow: Literal["all"] | None = None
    """``all`` copies every row of the parent table, not only the referenced
    ones. Give this or ``on_missing``, not both."""
    on_missing: OnMissing | None = None
    """What to do when the referenced parent row is not followed: ``null``,
    ``stub`` or ``drop``. Give this or ``follow: all``, not both."""

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
    """A reference the schema does not declare.

    In ``subset.virtual_fks``, each is keyed ``child.column -> parent``.
    Subsetting follows it like a declared foreign key, for every row or only
    for the rows ``when`` selects.
    """

    when: str | None = None
    """A condition on the child row, in the source's own query language. The
    reference holds only for rows that match it, such as
    ``subject_type = 'Post'`` for a polymorphic reference. Without it, the
    reference holds for every row."""


class JailerSubset(_Model):
    """A subset designed in Jailer's GUI, which Datadouble reads again on every run.

    The Jailer compatibility page explains what Datadouble takes from it.
    """

    model: Path
    """The Jailer extraction model, a ``.jm`` file."""
    datamodel: Path
    """The Jailer data model folder that the model was designed against."""


class Subset(_Model):
    """Which rows to copy.

    You give starting conditions, called seeds, and say which references to
    follow. The planner derives every other table's rows from them. Instead,
    ``jailer`` can name a subset designed in Jailer.
    """

    as_of: AwareDatetime | None = None
    """The anchor time that conditions refer to as ``:as_of``, with a time
    zone. The plan resolves it once, so every query uses the same time.
    Defaults to the time the plan runs."""
    strategy: Strategy = Strategy.AUTO
    """How the planner computes the subset: ``auto``, ``nested`` or ``keys``.
    Defaults to ``auto``. The ``--strategy`` option overrides it."""
    seeds: dict[str, str] = Field(default_factory=dict[str, str])
    """Starting conditions, in the source's own query language, by table. Keys
    are table names or globs, such as ``"*": "TRUE"``. A table's own name wins
    over a glob."""
    follow: Follow = Follow()
    """Which references to follow down from parent rows to child rows, listed
    under ``down``. Parents of copied rows are always copied."""
    edges: dict[str, EdgePolicy] = Field(default_factory=dict[str, EdgePolicy])
    """What to do with particular references, keyed ``child.column -> parent``:
    copy every parent row with ``follow: all``, or fall back with
    ``on_missing``."""
    virtual_fks: dict[str, VirtualForeignKey] = Field(default_factory=dict[str, VirtualForeignKey])
    """References the schema does not declare, keyed ``child.column -> parent``.
    Subsetting follows them like declared foreign keys."""
    unselected: Unselected | None = None
    """What happens to a kept table that no seed or followed reference selects:
    ``fail``, ``none`` or ``all``. Defaults to ``fail``."""
    jailer: JailerSubset | None = None
    """A subset designed in Jailer. It replaces ``seeds``, ``follow``,
    ``edges``, ``virtual_fks`` and ``unselected``, so a spec cannot give both."""

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
    """Replace each value with a deterministic fake value, such as ``{faker: email}``.

    The same input gets the same fake value of a kind in every table, so joins
    on the column still match. In a ``unique`` column, a value whose fake
    collides with another's gets a different one.
    """

    faker: FakerKind
    """The kind of fake value to generate, such as ``name`` or ``email``."""
    unique: bool = False
    """Whether every fake value in the column must be different. The generator
    retries until it finds an unused value, or fails the run. Defaults to
    false."""


class HashOptions(_Model):
    """Options for the ``hash`` strategy."""

    length: int = Field(default=16, ge=8, le=64)
    """How many hexadecimal characters the hash has, from 8 to 64. Defaults to
    16. In integer columns, the hash is a 32-bit integer instead."""


class HashRule(_Model):
    """Replace each value with a keyed hash, so equal inputs stay equal."""

    hash: HashOptions
    """Options for the hash. Write ``{}`` to take the defaults."""


class NullOptions(_Model):
    """The ``null`` strategy has no options, so it is written ``null: {}``."""


class NullRule(_Model):
    """Set each value to null."""

    null: NullOptions
    """No options: write ``{}``."""


class FormatPreservingOptions(_Model):
    """Options for the ``format_preserving`` strategy."""

    luhn: bool = False
    """Whether to keep the Luhn check digit valid, as card numbers need.
    Defaults to false."""


class FormatPreservingRule(_Model):
    """Replace digits with digits and letters with letters, so each value keeps its shape."""

    format_preserving: FormatPreservingOptions
    """Options for the strategy. Write ``{}`` to take the defaults."""


class ExpressionRule(_Model):
    """Replace each value with an engine-native expression over the row.

    A ``where`` condition can limit the change to some rows.
    """

    expression: str = Field(min_length=1)
    """The new value, as an expression in the engine's own language, such as
    SQL for PostgreSQL. Must not be empty."""
    where: str | None = None
    """A condition in the engine's own language. Only rows that match it
    change. Without it, every row changes."""


type Rule = Annotated[
    FakerRule | HashRule | NullRule | FormatPreservingRule | ExpressionRule,
    Field(
        union_mode="left_to_right",
        description=(
            "One column's anonymization rule. It names exactly one strategy: `faker`, "
            "`hash`, `null`, `format_preserving` or `expression`."
        ),
    ),
]


class Hook(_Model):
    """A native script run before load, such as one that sets server settings or adds extensions."""

    sql: str = Field(min_length=1)
    """The script, in the engine's own language. Must not be empty."""


class TransformHook(_Model):
    """A native script run after the column rules, which declares the columns it rewrites."""

    sql: str = Field(min_length=1)
    """The script, in the engine's own language. Must not be empty."""
    rewrites: tuple[str, ...] = ()
    """The columns the script rewrites, as ``table.column``. The plan fails if
    one names a column the source doesn't have, and the transform stage
    records them. Datadouble takes the list on trust, and ``verify`` checks
    the result."""


class Hooks(_Model):
    """Native scripts run around load and transform.

    Not every engine supports hooks. The plan fails if a spec gives
    hooks to an engine that doesn't.
    """

    before_load: tuple[Hook, ...] = ()
    """Scripts run in each working database before the copied data is loaded."""
    after_transform: tuple[TransformHook, ...] = ()
    """Scripts run after every column rule, and before ``package`` and
    ``verify``, so verification covers what they change."""


type ObjectRoot = Annotated[
    dict[ObjectKind, str],
    Field(
        min_length=1,
        max_length=1,
        description=(
            "An object that code outside Datadouble expects in an image, written as its "
            "kind and its name: `{function: public.uuid_generate_v4}`. Names match from "
            "the right, like table names. Where routines are overloaded, add the argument "
            'types as the catalog spells them: `{function: "public.total(integer)"}`. '
            "A PostgreSQL trigger's name is `table.trigger`, as in "
            "`{trigger: orders.audit}`."
        ),
    ),
]


class PulledRows(StrEnum):
    """Which rows of a table a standalone group pulls into its image.

    - ``referenced``: the rows that the image's rows reference, plus the rows
      that reference them where the spec follows references down. This is the
      default.
    - ``all``: every row of the source table.
    - ``none``: no rows, only the table's structure.
    """

    REFERENCED = "referenced"
    ALL = "all"
    NONE = "none"


class ImageGroup(_Model):
    """One image built from the shared extract.

    The image holds the tables that the group's ``include`` patterns match.
    """

    include: tuple[str, ...] = Field(min_length=1)
    """The tables in the image, as globs matched from the right. At least one
    is needed."""
    requires: tuple[GroupName, ...] = ()
    """Other groups that this group's references may point into. A reference
    from this group to rows in another group fails the plan unless that group
    is listed here, directly or through a listed group's own ``requires``, or
    the reference has ``on_missing: null``. A reference with ``on_missing:
    stub`` or ``drop`` cannot leave its group at all. Each listed group must
    be a group in ``images``. A standalone group cannot have any."""
    standalone: bool = False
    """Whether the image must work on its own. If true, package planning pulls
    into the image whatever its tables and objects need from outside the
    group. Defaults to false."""
    objects: tuple[ObjectRoot, ...] = ()
    """Objects that code outside Datadouble expects in this image, such as
    ``{function: public.uuid_generate_v4}``. They join the image's closure,
    the set of everything the image must contain."""
    rows: dict[str, PulledRows] = Field(default_factory=dict[str, PulledRows])
    """Which rows a standalone group pulls in, in place of the default
    ``referenced``. Keys are a table name or glob, or a reference
    ``child.column -> parent``, which wins over a table key. Values are
    ``referenced``, ``all`` or ``none``. Needs ``standalone: true``."""

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
    """The privileges of a copy account.

    - ``all``: every privilege, with the grant option. On PostgreSQL, this is
      a superuser. This is the default.
    - ``read``: ``SELECT`` on the image's data, plus what the engine gives
      every user.
    """

    ALL = "all"
    READ = "read"


class CopyAccount(_Model):
    """An account that every copy of a physical image carries, so consumers can log in over TCP."""

    user: str = Field(min_length=1)
    """The account's user name. Must not be empty."""
    password: Annotated[str, Field(min_length=1)] | SecretRef
    """The account's password: plain text, since copies hold no personal
    data, or a secret reference."""
    privileges: AccountPrivileges = AccountPrivileges.ALL
    """What the account may do: ``all`` (every privilege, with the grant
    option) or ``read`` (``SELECT`` on the image's data). Defaults to ``all``."""


class Package(_Model):
    """Which image formats to produce, and the database accounts built into physical images."""

    formats: tuple[ImageFormat, ...] | None = Field(default=None, min_length=1)
    """The image formats to produce: ``physical``, ``logical`` or both. If
    given, the list must not be empty. Defaults to every format the engine
    supports. The ``--format`` option overrides it."""
    accounts: tuple[CopyAccount, ...] = ()
    """Database accounts built into physical images, for consumers that serve
    them with the engine's own image. None by default."""


class PiiScan(_Model):
    """Values the PII scan accepts on purpose."""

    allow: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    """``LIKE`` patterns of values the scan accepts, by column, such as
    ``users.email: ["%@company.com"]``. A value that a detector flags and no
    pattern covers still fails verification."""


class Assertion(_Model):
    """A native query that must return no rows for the image to pass verification."""

    name: str = Field(min_length=1)
    """The assertion's name, as the verification report shows it. Must not be
    empty."""
    sql: str = Field(min_length=1)
    """The query, in the engine's own language. Any row it returns fails
    verification. Must not be empty."""


class Verify(_Model):
    """Verification settings, beyond the checks that always run."""

    pii_scan: PiiScan = PiiScan()
    """Values the PII scan accepts on purpose, listed under ``allow``."""
    assertions: tuple[Assertion, ...] = ()
    """Native queries that must return no rows."""


type AdapterSettings = Annotated[
    dict[str, dict[str, JsonValue]],
    Field(
        description=(
            "One adapter and its settings, as a mapping with a single key that names the "
            "adapter, such as `{s3: {bucket: ...}}`. The adapter checks its own settings, "
            "and its README lists them."
        )
    ),
]


def _one_adapter(value: AdapterSettings) -> AdapterSettings:
    if len(value) != 1:
        message = "name exactly one adapter, as a mapping with a single key"
        raise ValueError(message)
    return value


class CosignSigning(_Model):
    """Signing with cosign.

    Give exactly one of ``key``, to sign with a key pair, or ``keyless: true``,
    to sign with the CI system's identity.
    """

    key: SecretRef | None = None
    """The private key, in cosign's encrypted format, as a secret reference.
    Its password comes from the ``COSIGN_PASSWORD`` environment variable."""
    keyless: bool = False
    """Whether to sign without a key, through the CI system's identity.
    Defaults to false."""
    transparency_log: bool = True
    """Whether to record signatures in the transparency log. Defaults to true.
    Turn it off only where the log cannot be reached."""

    @model_validator(mode="after")
    def _one_way(self) -> "CosignSigning":
        if (self.key is None) != self.keyless:
            message = "give exactly one of `key` or `keyless: true`"
            raise ValueError(message)
        return self


class Signing(_Model):
    """How ``store`` signs images."""

    cosign: CosignSigning
    """Sign with cosign, using a key pair or the CI system's identity."""


class CosignTrust(_Model):
    """Which cosign signatures ``promote`` and ``fetch`` accept.

    Give either ``public_key``, for key-pair signatures, or both ``identity``
    and ``issuer``, for keyless signatures.
    """

    public_key: SecretRef | None = None
    """For key-pair signatures: the public key that signatures must verify
    against, as a secret reference."""
    identity: str | None = None
    """For keyless signatures: the signer's certificate identity, matched
    exactly. Give ``issuer`` with it."""
    issuer: str | None = None
    """For keyless signatures: the OpenID Connect (OIDC) issuer, matched
    exactly. Give ``identity`` with it."""
    transparency_log: bool = True
    """Whether to require a transparency log entry. Defaults to true. Turn it
    off only for signatures made without one."""

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
    """Accept cosign signatures made with a given key or identity."""


class Promote(_Model):
    """Where ``promote`` copies verified images, and what it requires of them."""

    to: tuple[AdapterSettings, ...] = Field(min_length=1)
    """The export stores, one adapter each, such as ``{filesystem: ...}`` or
    ``{s3: ...}``. At least one is needed. A filesystem store's ``mode``
    (``"0640"`` by default) decides who can read the files, such as a web
    server that serves them. Each store also gets an index of its images per
    group, ``<spec>/<group>/index.json``. Stores that cannot list keys, like
    ``http``, are read through that index."""
    channel: str = "latest"
    """The channel ``promote`` points at each image it copies to the export
    stores. Defaults to ``latest``. A channel name is 1 to 63 lowercase
    letters, digits, ``-`` or ``_``, starting with a letter or digit."""
    strict: Strict = Strict()
    """The least strict level, per kind, that an image may have been built
    with. ``promote`` refuses an image built with a lower level. By default,
    ``data: columns`` and ``warn`` for each kind of object."""

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
    """Where ``fetch`` reads images from, and where it keeps them."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        validate_by_name=True,
        use_attribute_docstrings=True,
    )

    source: AdapterSettings = Field(alias="from")
    """The export store to read, as one adapter: ``{filesystem: ...}``,
    ``{s3: ...}`` or ``{http: ...}``. The ``http`` store reads from a web
    server that serves the export store. It is read-only, and reads the index
    that ``promote`` writes."""
    staging: Path
    """The staging directory that ``fetch`` unpacks images into, for
    ``replicate`` to copy from."""
    channel: str = "latest"
    """The export stores' channel to fetch. Defaults to ``latest``. A channel
    name is 1 to 63 lowercase letters, digits, ``-`` or ``_``, starting with a
    letter or digit."""
    staging_channel: str = "active"
    """The channel in staging that ``fetch`` points at each new image.
    ``replicate`` copies from it, and ``activate`` moves it. Defaults to
    ``active``. It follows the same naming rules as ``channel``."""
    format: ImageFormat = ImageFormat.PHYSICAL
    """Which image format to fetch: ``physical`` or ``logical``. Defaults to
    ``physical``."""
    engine_version: str | None = None
    """Which compatibility line's images to fetch, such as ``8.4`` (not
    ``8.4.13``). Defaults to the newest line."""
    keep: int = Field(default=3, ge=1)
    """How many generations staging keeps, so ``activate`` can roll back to
    them. At least 1. Defaults to 3."""

    @field_validator("source")
    @classmethod
    def _store(cls, value: AdapterSettings) -> AdapterSettings:
        return _one_adapter(value)

    @field_validator("channel", "staging_channel")
    @classmethod
    def _channels(cls, value: str) -> str:
        return check_name("channel", value)


class ReplicateMode(StrEnum):
    """How ``replicate`` decides how many copies to make.

    - ``pool``: keep a number of unclaimed copies of the active generation
      ready. This is the default.
    - ``on-demand``: make copies for a named requester.
    """

    POOL = "pool"
    ON_DEMAND = "on-demand"


class Replicate(_Model):
    """How copies are made from staging."""

    using: AdapterSettings
    """The replicate adapter that makes the copies, such as
    ``{docker-volume: {...}}``. Its README lists its settings."""
    mode: ReplicateMode = ReplicateMode.POOL
    """How ``replicate`` decides how many copies to make: ``pool`` or
    ``on-demand``. Defaults to ``pool``."""
    size: int = Field(default=2, ge=1)
    """In pool mode, how many unclaimed copies to keep ready. In on-demand
    mode, how many copies each owner gets. At least 1. Defaults to 2."""
    expires_hours: int = Field(default=24, ge=1)
    """How many hours an on-demand copy lives before ``release --expired``
    removes it. At least 1. Defaults to 24."""

    @field_validator("using")
    @classmethod
    def _adapter(cls, value: AdapterSettings) -> AdapterSettings:
        return _one_adapter(value)


class Retention(_Model):
    """What ``prune`` keeps.

    An image line is one image group's images for one engine version, such as
    ``postgres-18``. For each image line, ``prune`` keeps the newest ``keep``
    generations, anything built within ``keep_days``, and whatever a channel
    points at. It never removes a channel's target.
    """

    keep: int = Field(default=3, ge=1)
    """How many of the newest generations each image line keeps. At least 1.
    Defaults to 3. In staging, ``fetch.keep`` applies instead."""
    keep_days: int = Field(default=7, ge=0)
    """Keep every generation built within this many days. 0 or more. Defaults
    to 7."""
    keep_versions: int = Field(default=2, ge=1)
    """How many of a group's newest engine versions keep their generations.
    Older versions keep only their channels' targets, and lose their unclaimed
    pool copies. At least 1. Defaults to 2."""


class Secrets(_Model):
    """Secrets the run needs, by reference."""

    anonymization_key: SecretRef | None = None
    """The key that seeds deterministic anonymization, as a secret reference.
    Without it, Datadouble generates a random key for each generation."""


class NotifyOn(StrEnum):
    """The outcomes of a generated CI job that send a notification.

    - ``failure``: the job failed.
    - ``warning``: the job finished with warnings.

    ``notify.when`` is ``[failure]`` by default.
    """

    FAILURE = "failure"
    WARNING = "warning"


class WebhookFormat(StrEnum):
    """The JSON body a webhook receives.

    - ``json``: every field (``spec``, ``job``, ``outcome``, ``generation``,
      ``error``, ``run_url``) and a ``text`` line built from them, for a
      receiver written for Datadouble.
    - ``chat``: only ``{"text": ...}``, for chat incoming webhooks (Google
      Chat, Slack, Mattermost, Rocket.Chat). Google Chat refuses any other
      key, so it needs ``chat``.

    ``format`` is ``json`` by default.
    """

    JSON = "json"
    CHAT = "chat"


class Webhook(_Model):
    """A URL that Datadouble posts a JSON summary of the job's outcome to."""

    url: SecretRef
    """The webhook's URL, as a secret reference, since the URL usually carries
    its credential."""
    format: WebhookFormat = WebhookFormat.JSON
    """The body to post: ``json`` (every field and a ``text`` line) or
    ``chat`` (only ``text``, for chat incoming webhooks)."""


class WebhookTarget(_Model):
    """A webhook target, such as ``{webhook: {url: {env: ALERTS_WEBHOOK}}}``."""

    webhook: Webhook
    """The webhook to post to."""


class Email(_Model):
    """Mail sent by the CI system's own mechanism, where it has one."""

    to: tuple[Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+$")], ...] = Field(min_length=1)
    """The addresses to mail. At least one is needed."""


class EmailTarget(_Model):
    """An email target, such as ``{email: {to: [data-team@example.com]}}``."""

    email: Email
    """The mail to send."""


type NotifyTarget = Annotated[
    WebhookTarget | EmailTarget,
    Field(description="Where one notification goes: `{webhook: ...}` or `{email: ...}`."),
]


class Notify(_Model):
    """Notifications for generated CI jobs that fail or warn (decision 125).

    Notifications live in the spec rather than in ``ci init``'s options, so
    generating the jobs again keeps them.
    """

    when: tuple[NotifyOn, ...] = Field(default=(NotifyOn.FAILURE,), min_length=1)
    """The outcomes that send a notification: ``failure``, ``warning`` or
    both. Defaults to ``[failure]``."""
    to: tuple[NotifyTarget, ...] = Field(min_length=1)
    """Where notifications go, one target each: ``{webhook: ...}`` or
    ``{email: ...}``. At least one is needed."""

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
    """A template that builds file paths from a table's columns.

    For example, ``path: "documents/{customer_id}/{file_name}"`` builds a path
    from two columns. ``match``, if given, is a regular expression applied to
    the result, whose first group is the path. Use it for values such as URLs.
    """

    table: str
    """The table whose selected rows hold the file paths."""
    path: str = Field(min_length=1)
    """A template that builds a value from the row's columns, such as
    ``documents/{customer_id}/{file_name}``. Without ``match``, the value is the
    path, relative to the tree's root. Must not be empty."""
    match: str | None = None
    """A regular expression applied to the value ``path`` builds. Its first
    group is the path. Use it for values such as URLs."""


FILES = EngineName("files")
"""The engine that copies a ``files`` section's tree."""
FILES_SOURCE = SourceName("files")
"""The name of the ``files`` section's build's one source."""


class Files(_Model):
    """A file tree that the source's rows refer to, copied beside them (decision 97).

    The ``files`` engine adapter reads the tree. Each top-level directory is a
    table, and ``select`` gives each one ``find`` tests. Every top-level
    directory must be selected or excluded.
    """

    connection: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Where the tree is and how to read it. The ``files`` adapter checks these
    settings."""
    select: dict[str, str] = Field(default_factory=dict[str, str])
    """The ``find`` tests that choose each top-level directory's files, such as
    ``-type f -mtime -30``."""
    exclude: tuple[str, ...] = ()
    """Top-level directories to leave out."""
    paths_from: str | PathsFrom | None = None
    """Which files the source's selected rows reference, relative to the
    tree's root. Either a ``table.column`` that holds paths, or a template
    with ``table``, ``path`` and optionally ``match``."""
    cleanup: tuple[str, ...] = ()
    """Shell commands run in the root of the tree's working copy during
    ``transform``, before the image is packaged and verified, such as one that
    removes EXIF data."""
    image: str | None = None
    """The container image that the cleanup commands run in. Defaults to a
    Debian image."""


class Spec(_Model):
    """A complete spec, which describes one pipeline."""

    model_config = ConfigDict(title="Datadouble spec")

    name: SpecName
    """The spec's name, the first part of every image's identity. It is 1 to
    63 lowercase letters, digits, ``-`` or ``_``, starting with a letter or
    digit."""
    sources: dict[SourceName, Source] = Field(min_length=1)
    """The servers to copy from, by name. At least one is needed, and every
    source uses the same engine. All sources' databases go into one working
    server, so their names must not collide. Each source is read under its own
    snapshot. Source names follow the same rules as the spec's name."""
    exclude: tuple[str, ...] = ()
    """Tables left out of every image, as globs matched from the right. For
    example, ``audit.*`` is every table in a namespace ``audit``, and
    ``shop.*.*`` is every table in PostgreSQL's database ``shop``."""
    structure_only: tuple[str, ...] = ()
    """Tables copied without their rows, as globs matched from the right."""
    case_sensitive: bool = False
    """Whether table and column names match case-sensitively. Defaults to
    false."""
    strict: Strict = Strict()
    """How strict mode treats differences from the catalog lock, as a mapping
    of levels per kind. ``data`` takes ``off``, ``tables`` or ``columns``.
    ``functions``, ``views``, ``triggers``, ``types``, ``sequences`` and
    ``events`` take ``off``, ``warn`` or ``fail``. Kinds left out take their
    defaults."""
    subset: Subset = Subset()
    """Which rows to copy: seeds, the references to follow, and what to do
    with the references not followed."""
    anonymize: dict[str, dict[str, Rule]] = Field(default_factory=dict[str, dict[str, Rule]])
    """Anonymization rules, by table and then by column. Each rule uses one
    strategy: ``faker``, ``hash``, ``null``, ``format_preserving`` or
    ``expression``."""
    hooks: Hooks = Hooks()
    """Native scripts run before load and after transform."""
    files: Files | None = None
    """A file tree copied with the rows, as a second source."""
    images: dict[GroupName, ImageGroup] = Field(
        default_factory=lambda: {DEFAULT_GROUP: ImageGroup(include=("*",))}
    )
    """The images built from one extract, by group name. Defaults to one
    group, ``all``, that holds every table. Group names follow the same rules
    as the spec's name."""
    objects: tuple[ObjectRoot, ...] = ()
    """Objects that code outside Datadouble expects in every image, such as
    ``{view: reporting.daily}``. They join each image's closure."""
    package: Package = Package()
    """The image formats to produce, and the database accounts built into physical images."""
    verify: Verify = Verify()
    """Checks every image must pass. Each image must restore, its row counts
    and references must match, the PII scan must find nothing it doesn't
    allow, and every assertion must return no rows. This section sets the PII
    scan's allowed values and the assertions."""
    runtime: AdapterSettings | None = None
    """Where engine software runs, from extract on: ``{docker: {...}}`` or
    ``{kubernetes: {...}}``. Extract and the stages after it need it. The
    ``kubernetes`` runtime reaches another cluster through ``kubeconfig``, a
    secret reference. It starts pods in the namespace given by ``namespace``.
    Without it, pods start where ``kubectl`` would put them: inside a cluster,
    the namespace of Datadouble's own pod, and otherwise the kubeconfig
    context's namespace."""
    store: tuple[dict[str, dict[str, JsonValue]], ...] = ()
    """Where the build stores images, one adapter each: ``{filesystem: ...}`` or
    ``{s3: ...}``. The filesystem store takes ``path``, a directory, and
    ``mode``, an octal string that defaults to ``"0640"``. Every file it writes
    gets ``mode``, whatever the umask. Directories it creates get ``mode`` plus
    the search bit wherever it has the read bit (``0750``). Modes that let
    anyone but the owner write, or that don't let the owner read and write, are
    refused."""
    signing: Signing | None = None
    """How the build signs its images."""
    trust: Trust | None = None
    """Which signatures ``promote`` and ``fetch`` accept."""
    promote: Promote | None = None
    """The export stores that ``promote`` copies verified images to, and what
    it requires of them."""
    fetch: Fetch | None = None
    """Where ``fetch`` reads promoted images, and the staging directory it
    fills."""
    replicate: Replicate | None = None
    """How copies are made: the adapter, the mode (pool or on-demand), and how
    many."""
    announce: tuple[AdapterSettings, ...] = ()
    """Where copies are announced to consumers, one adapter each:
    ``{dotenv: ...}``, ``{ci-outputs: {}}``, ``{vault: ...}`` or
    ``{k8s-secret: {}}``. The ``k8s-secret`` adapter writes Secrets in the
    namespace given by ``namespace``, and otherwise where ``kubectl`` would
    put them, as the ``kubernetes`` runtime does."""
    prune: Retention = Retention()
    """What ``prune`` keeps: how many generations, for how many days, and how
    many engine versions."""
    secrets: Secrets = Secrets()
    """Secrets the build needs, by reference."""
    notify: Notify | None = None
    """Notifications for generated CI jobs that fail or warn: webhooks and email."""

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
            (".".join(str(part) for part in problem["loc"]) or "(spec)", problem["msg"])
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
