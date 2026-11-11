# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The S3 store: error handling against a stubbed client, behavior against SeaweedFS."""

import os
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_s3 import S3Client

from datadouble.registry import installed
from datadouble.settings import SettingsError
from datadouble.storage import StoreError
from datadouble_s3 import S3Settings, S3Store, s3_store

ENDPOINT = "DATADOUBLE_TEST_S3_ENDPOINT"


def offline_client() -> S3Client:
    client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType] - only S3 stubs installed
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - a stub needs some credentials
    )
    return client


def test_settings() -> None:
    store = s3_store({"bucket": "images", "prefix": "/shop/", "endpoint_url": "http://x:1"})
    assert store.describe() == "s3://images/shop"
    assert s3_store({"bucket": "images"}).describe() == "s3://images"
    with pytest.raises(SettingsError, match="s3 store settings are invalid: bucket"):
        s3_store({"bucket": "x"})
    assert "s3" in installed().stores


def test_errors_name_the_code_not_the_request(tmp_path: Path) -> None:
    client = offline_client()
    store = S3Store(S3Settings(bucket="images"), client)
    source = tmp_path / "file"
    source.write_bytes(b"x")
    with Stubber(client) as stub:
        stub.add_client_error("put_object", service_error_code="AccessDenied")
        stub.add_client_error("get_object", service_error_code="AccessDenied")
        stub.add_client_error("get_object", service_error_code="NoSuchKey")
        stub.add_client_error("list_objects_v2", service_error_code="NoSuchBucket")
        with pytest.raises(StoreError, match="writing a/b failed: S3 error AccessDenied"):
            store.put_bytes("a/b", b"x")
        with pytest.raises(StoreError, match="reading a/b failed: S3 error AccessDenied"):
            store.get_bytes("a/b")
        assert store.get_bytes("a/b") is None
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_client_error("delete_object", service_error_code="AccessDenied")
        with pytest.raises(StoreError, match="listing / failed: S3 error NoSuchBucket"):
            store.list("")
        with pytest.raises(StoreError, match="downloading a/b failed: S3 error 403"):
            store.get_file("a/b", tmp_path / "out")
        assert not store.get_file("a/b", tmp_path / "out")
        with pytest.raises(StoreError, match="deleting a/b failed: S3 error AccessDenied"):
            store.delete("a/b")
    with pytest.raises(StoreError, match="not a valid store key"):
        store.put_bytes("../escape", b"x")


def test_connection_failures(tmp_path: Path) -> None:
    client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType] - only S3 stubs installed
        "s3",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:9",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - a stub needs some credentials
        config=Config(retries={"max_attempts": 1}, connect_timeout=1),
    )
    store = S3Store(S3Settings(bucket="images"), client)
    source = tmp_path / "file"
    source.write_bytes(b"x")
    for action, call in (
        ("uploading", lambda: store.put_file("a", source)),
        ("writing", lambda: store.put_bytes("a", b"x")),
        ("reading", lambda: store.get_bytes("a")),
        ("downloading", lambda: store.get_file("a", tmp_path / "out")),
        ("listing", lambda: store.list("a")),
    ):
        with pytest.raises(StoreError, match=f"{action} a failed: EndpointConnectionError"):
            call()


@pytest.fixture
def bucket() -> S3Store:
    endpoint = os.environ.get(ENDPOINT)
    if not endpoint:
        pytest.skip(f"{ENDPOINT} is not set; run in the Compose default group")
    name = f"dd-{uuid.uuid4().hex[:12]}"
    store = s3_store({"bucket": name, "prefix": "images", "endpoint_url": endpoint})
    assert isinstance(store, S3Store)
    store.client.create_bucket(Bucket=name)
    return store


@pytest.mark.integration
def test_round_trips(bucket: S3Store, tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.write_bytes(os.urandom(9 * 1024 * 1024))
    bucket.put_file("shop/all/a.tar.gz", big)
    bucket.put_bytes("shop/all/a.summary.json", b"{}")
    assert bucket.get_bytes("shop/all/a.summary.json") == b"{}"
    assert bucket.get_bytes("shop/all/missing") is None
    assert bucket.get_file("shop/all/a.tar.gz", tmp_path / "copy")
    assert (tmp_path / "copy").read_bytes() == big.read_bytes()
    assert not bucket.get_file("shop/all/missing", tmp_path / "none")
    assert bucket.list("shop/") == ["shop/all/a.summary.json", "shop/all/a.tar.gz"]
    assert bucket.list("") == ["shop/all/a.summary.json", "shop/all/a.tar.gz"]
    assert bucket.list("other/") == []
    bucket.delete("shop/all/a.summary.json")
    bucket.delete("shop/all/never-there")
    assert bucket.list("shop/") == ["shop/all/a.tar.gz"]


@pytest.mark.integration
def test_many_keys_page(bucket: S3Store) -> None:
    for number in range(1005):
        bucket.put_bytes(f"many/{number:04d}", b"")
    keys = bucket.list("many/")
    assert len(keys) == 1005
    assert keys[0] == "many/0000"
