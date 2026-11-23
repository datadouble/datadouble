# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Kubernetes adapters against a stand-in kubectl, and against k3s when it is running."""

import datetime as dt
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from datadouble.announce import Announced, AnnounceError
from datadouble.ci import JobKind, PipelineOptions, masking, pipeline
from datadouble.images import ImageIdentity, ImageSummary, Verification
from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.registry import installed
from datadouble.replicate import ClaimState, Copy, CopyKind, CopyState, ReplicateError
from datadouble.secrets import SecretError
from datadouble.settings import SettingsError
from datadouble.spec import EnvSecret, FileSecret, ReplicateMode, StrictLevel, parse_spec
from datadouble_kubernetes import (
    Kubectl,
    KubectlError,
    LocalVolume,
    PvReplicator,
    PvSettings,
    SecretAnnouncer,
    SecretSettings,
    pv_replicator,
    secret_announcer,
    secret_values,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

FAKE_KUBECTL = """
import json, os, stat, sys
from pathlib import Path
state = Path(sys.argv[0]).with_name("state.json")
objects = json.loads(state.read_text()) if state.exists() else {}
options = [a for a in sys.argv[1:] if a.startswith(("--kubeconfig=", "--context="))]
args = [a for a in sys.argv[1:] if a not in options]
call = {"verb": args[0]}
for option in options:
    name, value = option[2:].split("=", 1)
    call[name] = value
    if name == "kubeconfig":
        call["content"] = Path(value).read_text()
        call["mode"] = stat.S_IMODE(os.stat(value).st_mode)
        call["directory_mode"] = stat.S_IMODE(os.stat(Path(value).parent).st_mode)
with state.with_name("calls.jsonl").open("a") as calls:
    calls.write(json.dumps(call) + "\\n")
if "garbled" in objects:
    print("quoting " + call.get("content", "").splitlines()[-1])
    sys.stderr.write("error: cannot parse " + call.get("content", "").splitlines()[-1] + "\\n")
    sys.exit(1)
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
    assert "datadouble.dev/server-version" not in metadata["labels"]
    # A summary naming the exact server labels the volume with it too.
    exact_summary = SUMMARY.model_copy(update={"server_version": "18.3"})
    replicator.create("def", image(tmp_path / "second"), exact_summary)
    exact = objects(cluster)["PersistentVolume/datadouble-def"]["metadata"]
    assert isinstance(exact, dict)
    assert exact["labels"]["datadouble.dev/server-version"] == "18.3"
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


def calls(state: Path) -> list[dict[str, object]]:
    lines = state.with_name("calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def kubeconfig() -> str:
    token = secrets.token_hex(16)
    return f"apiVersion: v1\nkind: Config\nusers:\n- name: ci\n  user:\n    token: {token}"


def test_kubeconfig_and_context(
    tmp_path: Path, cluster: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    content = kubeconfig()
    monkeypatch.setenv("SHOP_KUBECONFIG", content)
    replicator = pv_replicator(
        {
            "path": str(tmp_path / "clones"),
            "local": {"node": "worker-1"},
            "kubeconfig": {"env": "SHOP_KUBECONFIG"},
            "context": "staging",
        }
    )
    replicator.create("abc", image(tmp_path), SUMMARY)
    assert replicator.claim_state(record("abc")) is ClaimState.FREE
    replicator.destroy(record("abc"))
    mounted = tmp_path / "mounted-secret"
    mounted.write_text(content + "\n", encoding="utf-8")
    announcer = secret_announcer({"kubeconfig": {"file": str(mounted)}})
    copy = Announced(copy_id="abc", kind="volume", labels={}, location={"path": "/x"})
    announcer.announce((copy,))
    announcer.withdraw(("abc",))
    made = calls(cluster)
    assert [call["verb"] for call in made] == ["apply", "get", "delete", "apply", "delete"]
    for call in made:
        assert call["content"] == content + "\n"
        assert call["mode"] == 0o600
        assert call["directory_mode"] == 0o700
    assert [call.get("context") for call in made] == ["staging"] * 3 + [None] * 2
    assert list(private.iterdir()) == []

    token = content.rsplit(" ", 1)[1]
    cluster.write_text(json.dumps({"garbled": {}}))
    with pytest.raises(ReplicateError, match=r"kubectl apply failed: .*\[redacted\]") as failed:
        replicator.create("def", image(tmp_path / "again"), SUMMARY)
    assert token not in str(failed.value)
    assert not (tmp_path / "clones" / "def").exists()
    assert list(private.iterdir()) == []
    done = Kubectl(EnvSecret(env="SHOP_KUBECONFIG")).run(["get"])
    assert token not in done.stdout + done.stderr
    assert done.stdout == "quoting     token: [redacted]\n"

    monkeypatch.delenv("SHOP_KUBECONFIG")
    unresolved = pv_replicator(
        {"path": str(tmp_path / "clones"), "local": {"node": "worker-1"},
         "kubeconfig": {"env": "SHOP_KUBECONFIG"}}
    )  # fmt: skip
    with pytest.raises(SecretError, match="SHOP_KUBECONFIG is not set"):
        unresolved.create("ghi", image(tmp_path / "third"), SUMMARY)
    assert not (tmp_path / "clones" / "ghi").exists()
    with pytest.raises(SettingsError, match="context"):
        secret_announcer({"context": ""})


def test_secret_values() -> None:
    token = secrets.token_hex(16)
    yaml_config = (
        f"apiVersion: v1\nkind: Config\nclusters:\n- name: shop\n  cluster:\n"
        f"    server: https://k8s.example.com:6443\n    insecure-skip-tls-verify: true\n"
        f"users:\n- name: ci\n  user:\n"
        f"    token: '{token}'\n    password: \"{token[:20]}\"\n"
    )
    assert set(secret_values(yaml_config)) == {"https://k8s.example.com:6443", token, token[:20]}
    json_config = json.dumps({"kind": "Config", "users": [{"user": {"token": token}}]})
    assert token in secret_values(json_config)
    assert token in secret_values(json.dumps({"users": [{"user": {"token": token}}]}, indent=2))
    broken = f"users: [\n  token: {token}\n"
    assert secret_values(broken) == (token,)
    assert secret_values("kind: Config\nusers: []") == ()


def test_only_secret_values_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    content = kubeconfig()
    monkeypatch.setenv("SHOP_KUBECONFIG", content)
    masked: list[str] = []
    with masking(masked.append):
        Kubectl(EnvSecret(env="SHOP_KUBECONFIG")).resolve()
    assert masked == [content.rsplit(" ", 1)[1]]


def test_kubeconfigs_stay_out_of_mounted_paths(
    tmp_path: Path, cluster: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted = tmp_path / "work"
    (mounted / "tmp").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(mounted / "tmp"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHOP_KUBECONFIG", kubeconfig())
    configured = Kubectl(EnvSecret(env="SHOP_KUBECONFIG"), avoid=mounted)
    configured(["delete", "secret", "x"])
    (call,) = calls(cluster)
    assert str(call["kubeconfig"]).startswith(str(home))
    assert list(home.iterdir()) == []
    monkeypatch.setenv("HOME", str(mounted))
    with pytest.raises(KubectlError, match="set TMPDIR elsewhere"):
        configured(["delete", "secret", "x"])


def test_ambient_configuration(cluster: Path) -> None:
    secret_announcer({"context": "dev"}).withdraw(("abc",))
    secret_announcer({}).withdraw(("abc",))
    assert calls(cluster) == [{"verb": "delete", "context": "dev"}, {"verb": "delete"}]


def test_kubectl_must_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATADOUBLE_KUBECTL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(KubectlError, match="kubectl was not found"):
        Kubectl()(["version"])
    registry = installed()
    assert "k8s-pv" in registry.replicators
    assert "k8s-secret" in registry.announcers


@pytest.mark.k8s
def test_against_k3s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("KUBECONFIG"):
        pytest.skip("no cluster; run with the k8s Compose profile")
    # The adapters reach the cluster only through their kubeconfig setting.
    context = Kubectl()(["config", "current-context"]).strip()
    configured = tmp_path / "kubeconfig"
    configured.write_text(Path(os.environ["KUBECONFIG"]).read_text("utf-8"), "utf-8")
    monkeypatch.setenv("K3S_KUBECONFIG", configured.read_text("utf-8"))
    monkeypatch.delenv("KUBECONFIG")
    replicator = pv_replicator(
        {
            "path": str(tmp_path / "clones"),
            "local": {"node": "k3s"},
            "kubeconfig": {"env": "K3S_KUBECONFIG"},
            "context": context,
        }
    )
    configured_kubectl = Kubectl(FileSecret(file=configured), context)
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
    configured_kubectl(["apply", "--filename=-"], stdin=json.dumps(claim))
    configured_kubectl(
        ["wait", "--for=jsonpath={.status.phase}=Bound", "pvc/dev-clone", "--timeout=60s"]
    )
    assert replicator.claim_state(copy) is ClaimState.CLAIMED
    configured_kubectl(["delete", "pvc/dev-clone", "--wait=true"])
    configured_kubectl(
        ["wait", "--for=jsonpath={.status.phase}=Released", "pv/datadouble-k3s1", "--timeout=60s"]
    )
    assert replicator.claim_state(copy) is ClaimState.RETURNED
    replicator.destroy(copy)
    announcer = secret_announcer({"kubeconfig": {"file": str(configured)}, "context": context})
    announcer.announce(
        (Announced(copy_id="k3s1", kind="volume", labels={}, location={"persistent_volume": "x"}),)
    )
    assert (
        configured_kubectl(["get", "secret", "datadouble-k3s1", "--output=name"]).strip()
        == "secret/datadouble-k3s1"
    )
    announcer.withdraw(("k3s1",))


CLUSTER_SPEC = """
name: shop
source:
  engine: postgres
  connection:
    dsn:
      env: SHOP_SOURCE_DSN
store:
  - filesystem:
      path: /srv/datadouble/store
promote:
  to:
    - filesystem:
        path: /srv/datadouble/export
fetch:
  from:
    filesystem:
      path: /srv/datadouble/export
  staging: /srv/datadouble/staging
runtime:
  kubernetes:
    claim: work
    mount: /srv/datadouble
    kubeconfig:
      env: BUILD_KUBECONFIG
replicate:
  using:
    k8s-pv:
      path: /srv/datadouble/copies
      local:
        node: worker-1
      kubeconfig:
        env: COPIES_KUBECONFIG
      context: dev
  mode: pool
  size: 1
announce:
  - k8s-secret:
      namespace: dev
      kubeconfig:
        file: /run/secrets/kubeconfig
"""


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ((JobKind.BUILD,), {JobKind.BUILD: {"BUILD_KUBECONFIG", "SHOP_SOURCE_DSN"}}),
        (
            (JobKind.PROMOTE, JobKind.PRUNE),
            {JobKind.PROMOTE: set[str](), JobKind.PRUNE: set[str]()},
        ),
        (
            (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.PRUNE, JobKind.ACTIVATE),
            {
                JobKind.FETCH: set[str](),
                JobKind.PROVISION: {"BUILD_KUBECONFIG", "COPIES_KUBECONFIG"},
                JobKind.RELEASE: {"BUILD_KUBECONFIG", "COPIES_KUBECONFIG"},
                JobKind.PRUNE: {"COPIES_KUBECONFIG"},
                JobKind.ACTIVATE: set[str](),
            },
        ),
    ],
)
def test_jobs_get_only_their_own_kubeconfigs(
    kinds: tuple[JobKind, ...], expected: dict[JobKind, set[str]]
) -> None:
    options = PipelineOptions(
        kinds=kinds,
        spec_path="datadouble/shop.yml",
        image="datadouble:test",
        version="1.2.3",
        owner="999:999",
        runner="runner",
        work=Path("/srv/datadouble/work"),
    )
    planned = pipeline(parse_spec(CLUSTER_SPEC), options)
    assert {job.kind: set(job.environment) for job in planned.jobs} == expected
    announcing = [j for j in planned.jobs if j.kind in (JobKind.PROVISION, JobKind.RELEASE)]
    for job in announcing:
        assert "/run/secrets/kubeconfig" in job.mounts
