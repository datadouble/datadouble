# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Kubernetes adapters against a stand-in kubectl, and against k3s when it is running."""

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from datadouble.announce import Announced, AnnounceError
from datadouble.images import ImageIdentity, ImageSummary, Verification
from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.registry import installed
from datadouble.replicate import ClaimState, Copy, CopyKind, CopyState, ReplicateError
from datadouble.settings import SettingsError
from datadouble.spec import ReplicateMode, StrictLevel
from datadouble_kubernetes import (
    KubectlError,
    LocalVolume,
    PvReplicator,
    PvSettings,
    SecretAnnouncer,
    SecretSettings,
    kubectl,
    pv_replicator,
    secret_announcer,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

FAKE_KUBECTL = """
import json, sys
from pathlib import Path
state = Path(sys.argv[0]).with_name("state.json")
objects = json.loads(state.read_text()) if state.exists() else {}
args = sys.argv[1:]
if "fail" in objects:
    sys.stderr.write("error: the server is on fire\\n")
    sys.exit(1)
if args[0] == "apply":
    obj = json.load(sys.stdin)
    key = obj["kind"] + "/" + obj["metadata"]["name"]
    objects[key] = obj
    print(key)
elif args[0] == "get":
    obj = objects.get("PersistentVolume/" + args[2])
    print(obj.get("phase", "Available") if obj else "", end="")
elif args[0] == "delete":
    kind = {"persistentvolume": "PersistentVolume", "secret": "Secret"}[args[1]]
    for name in [a for a in args[2:] if not a.startswith("--")]:
        objects.pop(kind + "/" + name, None)
state.write_text(json.dumps(objects))
"""

SUMMARY = ImageSummary(
    identity=ImageIdentity(
        spec=SpecName("shop"),
        group=GroupName("all"),
        engine=EngineName("postgres"),
        engine_version="18",
        generation=Generation("0193a7c21b4e9f30"),
    ),
    built_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
    archives=(),
    manifest_sha256="0" * 64,
    verification=Verification(passed=True, checks=1, failed=0),
    strict=StrictLevel.COLUMNS,
)


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "bin" / "kubectl"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("DATADOUBLE_KUBECTL", str(script))
    return script.with_name("state.json")


def objects(state: Path) -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads(state.read_text())
    return data


def set_phase(state: Path, name: str, phase: str) -> None:
    data = objects(state)
    data[f"PersistentVolume/{name}"]["phase"] = phase
    state.write_text(json.dumps(data))


def record(copy_id: str) -> Copy:
    return Copy(
        id=copy_id,
        prefix="shop/all/postgres-18",
        generation=Generation("0193a7c21b4e9f30"),
        kind=CopyKind.VOLUME,
        mode=ReplicateMode.POOL,
        adapter="k8s-pv",
        state=CopyState.READY,
        created_at=SUMMARY.built_at,
        built_at=SUMMARY.built_at,
    )


def image(tmp_path: Path) -> Path:
    staged = tmp_path / "image"
    (staged / "pgdata").mkdir(parents=True)
    (staged / "pgdata" / "PG_VERSION").write_text("18\n", encoding="utf-8")
    return staged


def test_local_volumes(tmp_path: Path, cluster: Path) -> None:
    replicator = PvReplicator(
        PvSettings(path=tmp_path / "clones", local=LocalVolume(node="worker-1"))
    )
    assert replicator.kind is CopyKind.VOLUME
    made = replicator.create("abc", image(tmp_path), SUMMARY)
    assert made.location == {
        "persistent_volume": "datadouble-abc",
        "storage_class": "datadouble",
        "path": str(tmp_path / "clones" / "abc"),
    }
    assert (tmp_path / "clones" / "abc" / "pgdata" / "PG_VERSION").is_file()
    volume = objects(cluster)["PersistentVolume/datadouble-abc"]
    spec = volume["spec"]
    assert isinstance(spec, dict)
    assert spec["local"] == {"path": str(tmp_path / "clones" / "abc")}
    assert spec["persistentVolumeReclaimPolicy"] == "Retain"
    metadata = volume["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["labels"]["datadouble.dev/engine-version"] == "18"
    copy = record("abc")
    assert replicator.claim_state(copy) is ClaimState.FREE
    set_phase(cluster, "datadouble-abc", "Bound")
    assert replicator.claim_state(copy) is ClaimState.CLAIMED
    set_phase(cluster, "datadouble-abc", "Released")
    assert replicator.claim_state(copy) is ClaimState.RETURNED
    replicator.destroy(copy)
    assert "PersistentVolume/datadouble-abc" not in objects(cluster)
    assert not (tmp_path / "clones" / "abc").exists()
    assert replicator.claim_state(copy) is ClaimState.RETURNED


def test_nfs_volumes_and_failures(tmp_path: Path, cluster: Path) -> None:
    settings: dict[str, JsonValue] = {
        "path": str(tmp_path / "clones"),
        "nfs": {"server": "nfs.internal", "path": "/exports/"},
    }
    replicator = pv_replicator(settings)
    replicator.create("def", image(tmp_path), SUMMARY)
    spec = objects(cluster)["PersistentVolume/datadouble-def"]["spec"]
    assert isinstance(spec, dict)
    assert spec["nfs"] == {"server": "nfs.internal", "path": "/exports/def"}
    with pytest.raises(ReplicateError, match="copying 0193a7c21b4e9f30 failed"):
        replicator.create("ghi", tmp_path / "missing", SUMMARY)
    cluster.write_text(json.dumps({"fail": {}}))
    with pytest.raises(ReplicateError, match="kubectl apply failed: error: the server is on fire"):
        replicator.create("jkl", image(tmp_path / "again"), SUMMARY)
    assert not (tmp_path / "clones" / "jkl").exists()
    with pytest.raises(ReplicateError, match="kubectl delete failed"):
        replicator.destroy(record("def"))
    with pytest.raises(ReplicateError, match="kubectl get failed"):
        replicator.claim_state(record("def"))
    with pytest.raises(SettingsError, match="exactly one of `local` or `nfs`"):
        pv_replicator({"path": "/x"})


def test_secrets(cluster: Path) -> None:
    announcer = SecretAnnouncer(SecretSettings(namespace="dev"))
    copy = Announced(
        copy_id="abc",
        kind="instance",
        labels={"spec": "shop", "owner": "mr 12/feature"},
        location={"host": "db", "port": 5432},
        secrets={"password": "s3cret"},
    )
    assert announcer.announce((copy,)) == ("k8s-secret:dev/datadouble-abc",)
    secret = objects(cluster)["Secret/datadouble-abc"]
    assert secret["stringData"] == {
        "host": "db",
        "port": "5432",
        "password": "s3cret",
        "copy_id": "abc",
    }
    metadata = secret["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["labels"]["datadouble.dev/owner"] == "mr-12-feature"
    assert announcer.withdraw(("abc",)) == ("k8s-secret:dev/datadouble-abc",)
    assert objects(cluster) == {}
    assert announcer.withdraw(()) == ()
    cluster.write_text(json.dumps({"fail": {}}))
    with pytest.raises(AnnounceError, match="kubectl apply failed"):
        announcer.announce((copy,))
    with pytest.raises(AnnounceError, match="kubectl delete failed"):
        announcer.withdraw(("abc",))
    assert isinstance(secret_announcer({}), SecretAnnouncer)


def test_kubectl_must_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATADOUBLE_KUBECTL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(KubectlError, match="kubectl was not found"):
        kubectl(["version"])
    registry = installed()
    assert "k8s-pv" in registry.replicators
    assert "k8s-secret" in registry.announcers


@pytest.mark.k8s
def test_against_k3s(tmp_path: Path) -> None:
    if not os.environ.get("KUBECONFIG"):
        pytest.skip("no cluster; run with the k8s Compose profile")
    replicator = PvReplicator(PvSettings(path=tmp_path / "clones", local=LocalVolume(node="k3s")))
    replicator.create("k3s1", image(tmp_path), SUMMARY)
    copy = record("k3s1")
    assert replicator.claim_state(copy) is ClaimState.FREE
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "dev-clone", "namespace": "default"},
        "spec": {
            "storageClassName": "datadouble",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
            "selector": {"matchLabels": {"datadouble.dev/copy": "k3s1"}},
        },
    }
    kubectl(["apply", "--filename=-"], stdin=json.dumps(claim))
    kubectl(["wait", "--for=jsonpath={.status.phase}=Bound", "pvc/dev-clone", "--timeout=60s"])
    assert replicator.claim_state(copy) is ClaimState.CLAIMED
    kubectl(["delete", "pvc/dev-clone", "--wait=true"])
    kubectl(
        ["wait", "--for=jsonpath={.status.phase}=Released", "pv/datadouble-k3s1", "--timeout=60s"]
    )
    assert replicator.claim_state(copy) is ClaimState.RETURNED
    replicator.destroy(copy)
    announcer = SecretAnnouncer(SecretSettings())
    announcer.announce(
        (Announced(copy_id="k3s1", kind="volume", labels={}, location={"persistent_volume": "x"}),)
    )
    assert (
        kubectl(["get", "secret", "datadouble-k3s1", "--output=name"]).strip()
        == "secret/datadouble-k3s1"
    )
    announcer.withdraw(("k3s1",))
