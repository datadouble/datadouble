# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Golden images: identity, packing, and the image summary stored beside each one.

An image is identified by spec, group, engine, engine version and generation.
Each requested format is packed as its own compressed tar archive holding the
engine's files under ``image/`` and the build manifest as ``manifest.json``.
The image summary, a few kilobytes of JSON, sits next to the archives so it
can be read without downloading them:

    shop/all/postgres-18/
      0193a7c21b4e9f30.logical.tar.gz
      0193a7c21b4e9f30.physical.tar.gz
      0193a7c21b4e9f30.summary.json
"""

import hashlib
import secrets
import tarfile
import time
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.spec import ImageFormat

CODEC: Literal["gz"] = "gz"
"""Compression for image archives (gzip, from the standard library)."""
_CHUNK = 1 << 20


class ImageError(Exception):
    """An image archive that cannot be written or read."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def new_generation(now_ms: int | None = None) -> Generation:
    """A generation ID: 12 hex digits of milliseconds since 1970, then 4 random ones.

    IDs sort by build time.
    """
    millis = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    return Generation(f"{millis:012x}{secrets.token_hex(2)}")


class ImageIdentity(_Model):
    """What an image is: the parts consumers select by."""

    spec: SpecName
    group: GroupName
    engine: EngineName
    engine_version: str
    """The major version of the server that restores the image."""
    generation: Generation

    def prefix(self) -> str:
        """The store directory holding this image's generations."""
        return f"{self.spec}/{self.group}/{self.engine}-{self.engine_version}"

    def archive_key(self, image_format: ImageFormat) -> str:
        """The store key of one format's archive."""
        return f"{self.prefix()}/{self.generation}.{image_format}.tar.{CODEC}"

    def summary_key(self) -> str:
        """The store key of the image summary."""
        return f"{self.prefix()}/{self.generation}.summary.json"


class Digest(_Model):
    """A file's SHA-256 and size."""

    sha256: str
    size: int


def digest(path: Path) -> Digest:
    """Hash a file."""
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            sha.update(chunk)
    return Digest(sha256=sha.hexdigest(), size=path.stat().st_size)


class ImageArchive(_Model):
    """One format of an image."""

    format: ImageFormat
    key: str
    codec: Literal["gz"] = CODEC
    digest: Digest


class Verification(_Model):
    """The outcome of ``verify``, as summarized for consumers."""

    passed: bool
    checks: int
    failed: int


class ImageSummary(_Model):
    """The small file stored beside each image, readable without downloading it."""

    summary_format: Literal[1] = 1
    identity: ImageIdentity
    built_at: AwareDatetime
    archives: tuple[ImageArchive, ...]
    manifest_sha256: str
    verification: Verification
    signature: str | None = None
    """The signature reference; images are signed from M2 on."""


def pack(source: Path, manifest: bytes, destination: Path) -> Digest:
    """Write ``source``'s contents and ``manifest`` into a compressed tar archive."""
    manifest_file = destination.with_name(destination.name + ".manifest.json")
    manifest_file.write_bytes(manifest)
    try:
        with tarfile.open(destination, "w:gz", compresslevel=6) as archive:
            archive.add(manifest_file, arcname="manifest.json")
            archive.add(source, arcname="image")
    finally:
        manifest_file.unlink(missing_ok=True)
    return digest(destination)


def unpack(archive: Path, destination: Path) -> tuple[Path, bytes]:
    """Extract an archive safely; return the image directory and the manifest."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as opened:
            opened.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as error:
        message = f"cannot unpack {archive.name}: {error}"
        raise ImageError(message) from None
    manifest = destination / "manifest.json"
    image = destination / "image"
    if not manifest.is_file() or not image.is_dir():
        message = f"{archive.name} is not a Datadouble image"
        raise ImageError(message)
    return image, manifest.read_bytes()
