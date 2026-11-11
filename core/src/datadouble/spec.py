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

from datadouble.names import DEFAULT_GROUP, EngineName, GroupName, SpecName, check_name


class SpecError(Exception):
    """A spec that cannot be read or does not validate."""

    def __init__(self, message: str, details: tuple[tuple[str, str], ...] = ()) -> None:
        """Keep the per-field problems alongside the summary."""
        super().__init__(message)
        self.details = details


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class StrictLevel(StrEnum):
    """How strict mode treats tables and columns missing from the catalog lock."""

    OFF = "off"
    TABLES = "tables"
    COLUMNS = "columns"


class Strategy(StrEnum):
    """How the subset is computed (see the subsetting design)."""

    AUTO = "auto"
    """Nested queries when the selection graph is acyclic, otherwise keys."""
    NESTED = "nested"
    """Strategy A: one nested query per table, pushed to the source."""
    KEYS = "keys"
    """Strategy B: collect keys, compute the closure, then fetch rows by key."""


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
    """The system to extract from.

    ``connection`` is checked by the engine adapter, which knows its fields.
    """

    engine: EngineName
    connection: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


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


class Subset(_Model):
    """Starting filters and the policy for deriving everything else."""

    as_of: AwareDatetime | None = None
    strategy: Strategy = Strategy.AUTO
    seeds: dict[str, str] = Field(default_factory=dict[str, str])
    follow: Follow = Follow()
    edges: dict[str, EdgePolicy] = Field(default_factory=dict[str, EdgePolicy])
    virtual_fks: dict[str, VirtualForeignKey] = Field(default_factory=dict[str, VirtualForeignKey])


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


class ImageGroup(_Model):
    """One image's share of the extract."""

    include: tuple[str, ...] = Field(min_length=1)
    requires: tuple[GroupName, ...] = ()


class Package(_Model):
    """Which image formats to produce; all the engine supports when omitted."""

    formats: tuple[ImageFormat, ...] | None = Field(default=None, min_length=1)


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
    channel: str = "latest"
    strict: StrictLevel = StrictLevel.COLUMNS
    """The least strict strict-mode level an image may have been built with."""

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

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", validate_by_name=True)

    source: AdapterSettings = Field(alias="from")
    staging: Path
    channel: str = "latest"
    staging_channel: str = "active"
    format: ImageFormat = ImageFormat.PHYSICAL
    engine_version: str | None = None
    """Which engine version's images to fetch; the newest when omitted."""
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
    """Unclaimed copies to keep ready (pool), or copies per request (on-demand)."""
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


class Spec(_Model):
    """A whole spec."""

    model_config = ConfigDict(title="Datadouble spec")

    name: SpecName
    source: Source
    exclude: tuple[str, ...] = ()
    structure_only: tuple[str, ...] = ()
    case_sensitive: bool = False
    strict: StrictLevel = StrictLevel.COLUMNS
    subset: Subset = Subset()
    anonymize: dict[str, dict[str, Rule]] = Field(default_factory=dict[str, dict[str, Rule]])
    hooks: Hooks = Hooks()
    images: dict[GroupName, ImageGroup] = Field(
        default_factory=lambda: {DEFAULT_GROUP: ImageGroup(include=("*",))}
    )
    package: Package = Package()
    verify: Verify = Verify()
    store: tuple[dict[str, dict[str, JsonValue]], ...] = ()
    signing: Signing | None = None
    trust: Trust | None = None
    promote: Promote | None = None
    fetch: Fetch | None = None
    replicate: Replicate | None = None
    announce: tuple[AdapterSettings, ...] = ()
    prune: Retention = Retention()
    secrets: Secrets = Secrets()

    @field_validator("announce")
    @classmethod
    def _announcers(cls, value: tuple[AdapterSettings, ...]) -> tuple[AdapterSettings, ...]:
        return tuple(_one_adapter(entry) for entry in value)

    @field_validator("strict", mode="before")
    @classmethod
    def _yaml_off(cls, value: object) -> object:
        # YAML 1.1 reads `strict: off` as false.
        return StrictLevel.OFF.value if value is False else value

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
