# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The S3 storage adapter for Datadouble.

Spec settings::

    store:
      - s3:
          bucket: golden-images
          prefix: shop                                  # optional
          region: us-east-1                             # optional
          endpoint_url: https://s3.internal.example     # optional, S3-compatible services

Credentials come from the usual AWS sources and never appear in the spec.
"""

from collections.abc import Mapping
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_s3 import S3Client
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.settings import parse_settings
from datadouble.storage import Store, StoreError, check_key

_MISSING = frozenset({"NoSuchKey", "404", "NotFound"})


class S3Settings(BaseModel):
    """The settings of one ``s3`` store."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = ""
    region: str | None = None
    endpoint_url: str | None = None


def _failure(error: Exception, action: str) -> StoreError:
    """A store error naming the S3 error code, not the request or credentials."""
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "unknown")
        return StoreError(f"{action} failed: S3 error {code}")
    return StoreError(f"{action} failed: {type(error).__name__}")


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


class S3Store:
    """Images under one bucket and key prefix."""

    def __init__(self, settings: S3Settings, client: S3Client) -> None:
        """Use ``client`` for every request."""
        self.settings = settings
        self.client = client
        self.prefix = settings.prefix.strip("/")

    def _key(self, key: str) -> str:
        check_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def describe(self) -> str:
        """``s3://bucket/prefix``."""
        location = f"s3://{self.settings.bucket}"
        return f"{location}/{self.prefix}" if self.prefix else location

    def put_file(self, key: str, source: Path) -> None:
        """Upload a file; large files go up in parts, and only appear when complete."""
        try:
            self.client.upload_file(str(source), self.settings.bucket, self._key(key))
        except (BotoCoreError, ClientError) as error:
            raise _failure(error, f"uploading {key}") from None

    def put_bytes(self, key: str, data: bytes) -> None:
        """Write a small object."""
        try:
            self.client.put_object(Bucket=self.settings.bucket, Key=self._key(key), Body=data)
        except (BotoCoreError, ClientError) as error:
            raise _failure(error, f"writing {key}") from None

    def get_bytes(self, key: str) -> bytes | None:
        """Read a small object, or None if it does not exist."""
        try:
            response = self.client.get_object(Bucket=self.settings.bucket, Key=self._key(key))
        except ClientError as error:
            if _code(error) in _MISSING:
                return None
            raise _failure(error, f"reading {key}") from None
        except BotoCoreError as error:
            raise _failure(error, f"reading {key}") from None
        return response["Body"].read()

    def get_file(self, key: str, destination: Path) -> bool:
        """Download an object; False if it does not exist."""
        try:
            self.client.download_file(self.settings.bucket, self._key(key), str(destination))
        except ClientError as error:
            if _code(error) in _MISSING:
                return False
            raise _failure(error, f"downloading {key}") from None
        except BotoCoreError as error:
            raise _failure(error, f"downloading {key}") from None
        return True

    def delete(self, key: str) -> None:
        """Remove an object; S3 does not mind if it does not exist."""
        try:
            self.client.delete_object(Bucket=self.settings.bucket, Key=self._key(key))
        except (BotoCoreError, ClientError) as error:
            raise _failure(error, f"deleting {key}") from None

    def list(self, prefix: str) -> list[str]:
        """Keys starting with ``prefix``, relative to the store's own prefix."""
        root = f"{self.prefix}/" if self.prefix else ""
        keys: list[str] = []
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.settings.bucket, Prefix=root + prefix
            ):
                keys.extend(item.get("Key", "")[len(root) :] for item in page.get("Contents", []))
        except (BotoCoreError, ClientError) as error:
            raise _failure(error, f"listing {prefix or '/'}") from None
        return sorted(keys)


def s3_store(settings: Mapping[str, JsonValue]) -> Store:
    """The entry point named in ``datadouble.stores``."""
    parsed = parse_settings(S3Settings, settings, "s3 store")
    # boto3-stubs overloads client() for every AWS service; only S3's stubs are installed.
    client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType] - see above
        "s3",
        region_name=parsed.region,
        endpoint_url=parsed.endpoint_url,
        config=Config(s3={"addressing_style": "path"} if parsed.endpoint_url else {}),
    )
    return S3Store(parsed, client)
