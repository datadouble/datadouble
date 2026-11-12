# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Kubernetes container runtime, with a stand-in kubectl that runs pods as local processes.

The stand-in keeps each pod's manifest beside it, runs the pod's command under
a small supervisor that records its exit code, answers readiness probes by
running them, and treats ``kill -TERM 1`` as a signal to the supervisor.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from datadouble.runtime import Container, ContainerError
from datadouble_kubernetes import kubectl
from datadouble_kubernetes.runtime import (
    KubernetesRuntime,
    KubernetesRuntimeSettings,
    kubernetes_runtime,
)

SUPERVISOR = """
import signal, subprocess, sys
from pathlib import Path
pod = Path(sys.argv[1])
manifest = __import__("json").loads((pod / "manifest.json").read_text())
env = {"PATH": "/usr/bin:/bin"}
for item in manifest["spec"]["containers"][0]["env"]:
    if "value" in item:
        env[item["name"]] = item["value"]
    else:
        ref = item["valueFrom"]["secretKeyRef"]
        secret_file = pod.parent / ("secret-" + ref["name"] + ".json")
        secret = __import__("json").loads(secret_file.read_text())
        env[item["name"]] = secret["stringData"][ref["key"]]
with open(pod / "log", "w") as log:
    command = manifest["spec"]["containers"][0]["command"]
    child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
    signal.signal(signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM))
    code = child.wait()
(pod / "exit").write_text(str(code))
"""

FAKE_KUBECTL = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[0]).parent / "cluster"
root.mkdir(exist_ok=True)
args = [a for a in sys.argv[1:] if not a.startswith("--namespace")]
if (root / "fail").exists():
    sys.stderr.write("error: the server is on fire\\n")
    sys.exit(1)
verb = args[0]
if verb == "create":
    obj = json.load(sys.stdin)
    name = obj["metadata"]["name"]
    if obj["kind"] == "Secret":
        (root / ("secret-" + name + ".json")).write_text(json.dumps(obj))
    else:
        pod = root / name
        pod.mkdir()
        (pod / "manifest.json").write_text(json.dumps(obj))
        image = obj["spec"]["containers"][0]["image"]
        if image not in ("missing", "pending"):
            process = subprocess.Popen(
                [sys.executable, str(root.parent / "supervisor.py"), str(pod)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (pod / "pid").write_text(str(process.pid))
    print(obj["kind"].lower() + "/" + name + " created")
elif verb == "get" and args[1] == "pod":
    pod = root / args[2]
    manifest = json.loads((pod / "manifest.json").read_text())
    main = manifest["spec"]["containers"][0]
    status = {"podIP": "127.0.0.1"}
    if main["image"] == "pending" and not (pod / "seen").exists():
        (pod / "seen").write_text("")
    elif main["image"] in ("missing", "pending"):
        waiting = {"waiting": {"reason": "ErrImagePull"}}
        status["containerStatuses"] = [{"state": waiting, "ready": False}]
    elif (pod / "exit").exists():
        code = int((pod / "exit").read_text())
        terminated = {"terminated": {"exitCode": code}}
        status["containerStatuses"] = [{"state": terminated, "ready": False}]
    else:
        probe = main.get("readinessProbe", {}).get("exec", {}).get("command")
        ready = bool(probe) and subprocess.run(probe).returncode == 0
        status["containerStatuses"] = [{"state": {"running": {}}, "ready": ready}]
    print(json.dumps({"status": status}))
elif verb == "get" and args[1] == "pods":
    found = [p / "manifest.json" for p in root.iterdir()]
    items = [json.loads(m.read_text()) for m in found if m.exists()]
    print(json.dumps({"items": items}))
elif verb == "exec":
    pod = root / args[1]
    command = args[args.index("--") + 1:]
    if command == ["sh", "-c", "kill -TERM 1"]:
        if (pod / "pid").exists():
            os.kill(int((pod / "pid").read_text()), signal.SIGTERM)
    else:
        result = subprocess.run(command, capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
elif verb == "delete":
    kind, name = args[1], args[2]
    if kind == "secret":
        (root / ("secret-" + name + ".json")).unlink(missing_ok=True)
    else:
        pod = root / name
        if (pod / "pid").exists():
            try:
                os.killpg(int((pod / "pid").read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if pod.exists():
            for item in pod.iterdir():
                item.unlink()
            pod.rmdir()
elif verb == "logs":
    log = root / args[1] / "log"
    print(log.read_text() if log.exists() else "", end="")
elif verb == "port-forward":
    name = args[1].split("/")[1]
    if (root / "no-forward").exists():
        print("error: no", flush=True)
        sys.exit(1)
    print("Forwarding from 127.0.0.1:" + args[2][1:] + " -> " + args[2][1:], flush=True)
    time.sleep(3600)
"""

LOOP = "trap 'exit 0' TERM; touch {ready}; while :; do sleep 0.1; done"


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "kubectl"
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}", encoding="utf-8")
    script.chmod(0o755)
    (tmp_path / "supervisor.py").write_text(SUPERVISOR, encoding="utf-8")
    monkeypatch.setenv("DATADOUBLE_KUBECTL", str(script))
    return tmp_path / "cluster"


def runtime(tmp_path: Path, **settings: JsonValue) -> KubernetesRuntime:
    made = kubernetes_runtime({"claim": "work", "mount": str(tmp_path), **settings})
    assert isinstance(made, KubernetesRuntime)
    return made


def server(tmp_path: Path, script: str | None = None, image: str = "postgres:18") -> Container:
    ready = tmp_path / "ready"
    ready.unlink(missing_ok=True)
    return Container(
        image=image,
        command=("sh", "-c", script or LOOP.format(ready=ready)),
        owner=tmp_path / "load",
        mounts=(tmp_path / "load",),
        port=5432,
        ready=("test", "-f", str(ready)),
    )


def manifests(cluster: Path) -> list[dict[str, dict[str, str]]]:
    return [
        json.loads((pod / "manifest.json").read_text())
        for pod in cluster.iterdir()
        if (pod / "manifest.json").exists()
    ]


def test_jobs(tmp_path: Path, cluster: Path) -> None:
    made = runtime(tmp_path, node="worker-1", image_pull_secrets=["registry"])
    done = made.run(
        Container(
            image="postgres:18",
            command=("sh", "-c", 'echo "$MODE $TOKEN"; echo warn >&2; exit 3'),
            owner=tmp_path / "extract",
            mounts=(tmp_path / "extract",),
            env={"MODE": "plain"},
            secrets={"TOKEN": "s3cret"},
        )
    )
    assert (done.status, done.stdout, done.stderr) == (3, "plain s3cret\n", "warn\n")
    assert list(cluster.iterdir()) == []
    assert not (tmp_path / "extract").exists() or not any((tmp_path / "extract").iterdir())


def test_manifests(tmp_path: Path) -> None:
    made = runtime(tmp_path, node="worker-1", image_pull_secrets=["registry"], namespace="dd")
    container = Container(
        image="postgres:18", command=("true",), owner=tmp_path, env={"A": "b"}, secrets={"K": "v"}
    )
    pod = made.manifest("p", container, ["run"])
    assert pod["metadata"] == {
        "name": "p",
        "namespace": "dd",
        "labels": {"datadouble.dev/runtime": "true"},
        "annotations": {"datadouble.dev/owner": str(tmp_path)},
    }
    assert pod["spec"] == {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 300,
        "securityContext": {"runAsUser": os.getuid(), "runAsGroup": os.getgid()},
        "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": "work"}}],
        "containers": [
            {
                "name": "main",
                "image": "postgres:18",
                "command": ["run"],
                "env": [
                    {"name": "A", "value": "b"},
                    {"name": "K", "valueFrom": {"secretKeyRef": {"name": "p", "key": "K"}}},
                ],
                "volumeMounts": [{"name": "work", "mountPath": str(tmp_path)}],
            }
        ],
        "imagePullSecrets": [{"name": "registry"}],
        "nodeSelector": {"kubernetes.io/hostname": "worker-1"},
    }


def test_job_failures(tmp_path: Path, cluster: Path) -> None:
    made = runtime(tmp_path)
    with pytest.raises(ContainerError, match="missing could not start: ErrImagePull"):
        made.run(Container(image="missing", command=("true",), owner=tmp_path))
    with pytest.raises(ContainerError, match="pending could not start"):
        made.run(Container(image="pending", command=("true",), owner=tmp_path))
    with pytest.raises(ContainerError, match="is not on the work claim"):
        made.run(Container(image="x", command=("true",), owner=tmp_path, mounts=(Path("/etc"),)))
    cluster.mkdir(exist_ok=True)
    (cluster / "fail").write_text("")
    with pytest.raises(ContainerError, match=r"starting x: kubectl create failed: .*on fire"):
        made.run(Container(image="x", command=("true",), owner=tmp_path))


def test_servers(tmp_path: Path, cluster: Path) -> None:
    with runtime(tmp_path).serve(server(tmp_path)) as running:
        assert (running.endpoint.host, running.endpoint.port) == ("127.0.0.1", 5432)
        done = running.execute(["sh", "-c", 'echo "$WORD"'], {"WORD": "inside"})
        assert done.stdout.strip() == "inside"
        assert running.execute(["true"]).status == 0
    assert manifests(cluster) == []
    forwarded = runtime(tmp_path, access="port-forward")
    with forwarded.serve(server(tmp_path)) as running:
        assert (running.endpoint.host, running.endpoint.port) == ("127.0.0.1", 5432)


def test_server_failures(tmp_path: Path, cluster: Path) -> None:
    made = runtime(tmp_path)
    with (
        pytest.raises(ContainerError, match="stopped while starting: going away"),
        made.serve(server(tmp_path, "echo going away; exit 3")),
    ):
        pass
    never = server(tmp_path, "trap 'exit 0' TERM; while :; do sleep 0.1; done")
    with (
        pytest.raises(ContainerError, match="was not ready after 1 seconds"),
        runtime(tmp_path, ready_seconds=1).serve(never),
    ):
        pass
    with (
        pytest.raises(ContainerError, match="missing could not start"),
        made.serve(server(tmp_path, image="missing")),
    ):
        pass
    ready = tmp_path / "ready"
    unclean = server(
        tmp_path, f"trap 'echo bye; exit 4' TERM; touch {ready}; while :; do sleep 0.1; done"
    )
    with pytest.raises(ContainerError, match="exited with status 4: bye"), made.serve(unclean):
        pass
    stubborn = server(tmp_path, f"trap '' TERM; touch {ready}; while :; do sleep 0.1; done")
    with (
        pytest.raises(ContainerError, match="did not stop within 1 seconds"),
        runtime(tmp_path, stop_seconds=1).serve(stubborn),
    ):
        pass
    portless = Container(image="x", command=("true",), owner=tmp_path)
    with pytest.raises(ContainerError, match="needs a port"), made.serve(portless):
        pass
    (cluster / "no-forward").write_text("")
    with (
        pytest.raises(ContainerError, match=r"port-forward to .* failed"),
        runtime(tmp_path, access="port-forward").serve(server(tmp_path)),
    ):
        pass


def test_release(tmp_path: Path, cluster: Path) -> None:
    made = runtime(tmp_path)
    left = made.serve(server(tmp_path))
    left.__enter__()
    elsewhere = Container(
        image="x",
        command=("sh", "-c", "sleep 30"),
        owner=tmp_path / "other",
        port=1,
        ready=("false",),
    )
    made.create("datadouble-other", elsewhere, elsewhere.command)
    made.release(tmp_path / "load")
    names = [str(m["metadata"]["name"]) for m in manifests(cluster)]
    assert names == ["datadouble-other"]
    with pytest.raises(ContainerError, match="reading the pod"):
        left.__exit__(None, None, None)
    made.delete("datadouble-other")


def test_kubectl_must_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATADOUBLE_KUBECTL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ContainerError, match="kubectl was not found"):
        KubernetesRuntime(KubernetesRuntimeSettings(claim="w", mount=tmp_path)).invoke(["version"])


def test_unreadable_statuses() -> None:
    assert KubernetesRuntime.container_state({}) == (None, None, False)
    assert KubernetesRuntime.container_state({"containerStatuses": [1]}) == (None, None, False)


IMAGE = "postgres:18-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"


@pytest.mark.k8s
def test_against_k3s(tmp_path: Path) -> None:
    """Jobs and servers in k3s, sharing files through a claim on the work volume."""
    if not os.environ.get("KUBECONFIG"):
        pytest.skip("no cluster; run with the k8s Compose profile")
    volume = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": "datadouble-work"},
        "spec": {
            "storageClassName": "work",
            "capacity": {"storage": "1Gi"},
            "accessModes": ["ReadWriteOnce"],
            "hostPath": {"path": "/work"},
        },
    }
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "datadouble-work", "namespace": "default"},
        "spec": {
            "storageClassName": "work",
            "volumeName": "datadouble-work",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }
    for item in (volume, claim):
        kubectl(["apply", "--filename=-"], stdin=json.dumps(item))
    made = kubernetes_runtime(
        {"claim": "datadouble-work", "mount": "/work", "access": "port-forward", "node": "k3s"}
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    done = made.run(
        Container(
            image=IMAGE,
            command=("sh", "-c", f'id -u; echo "$TOKEN" > {shared}/out'),
            owner=tmp_path,
            mounts=(shared,),
            secrets={"TOKEN": "s3cret"},
        )
    )
    assert (done.status, done.stdout.strip()) == (0, str(os.getuid()))
    assert (shared / "out").read_text("utf-8") == "s3cret\n"
    ready = shared / "ready"
    serving = Container(
        image=IMAGE,
        command=("sh", "-c", LOOP.format(ready=ready)),
        owner=tmp_path,
        mounts=(shared,),
        port=5432,
        ready=("test", "-f", str(ready)),
    )
    with made.serve(serving) as running:
        assert running.endpoint.host == "127.0.0.1"
        assert running.execute(["sh", "-c", "echo inside"]).stdout.strip() == "inside"
