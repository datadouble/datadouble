# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The kind cluster helpers, with kind, kubectl and docker stood in for."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from datadouble.programs import Completed
from datadouble_conformance import ci, kind


class Programs:
    """Records commands; ``failing`` names the programs that fail."""

    def __init__(self, *failing: str, images: str = "") -> None:
        self.failing = set(failing)
        self.images = images
        self.calls: list[list[str]] = []

    def __call__(
        self,
        program: str,
        arguments: Sequence[str],
        *,
        stdin: str | bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        del env
        self.calls.append([program, *arguments, *([str(stdin)] if stdin else [])])
        if program in self.failing:
            return Completed(status=1, stdout="", stderr=f"{program}: broken\n")
        if program == "docker" and list(arguments[:2]) == ["image", "ls"]:
            return Completed(status=0, stdout=self.images, stderr="")
        if program == "docker" and arguments[0] == "save":
            Path(arguments[2]).write_bytes(b"archive")
        return Completed(status=0, stdout="172.18.0.1\n", stderr="")


@pytest.fixture
def programs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Programs:
    recorded = Programs()
    monkeypatch.setattr(kind, "run_program", recorded)
    monkeypatch.setattr(ci, "run_program", recorded)
    monkeypatch.setenv(ci.WORK_VARIABLE, str(tmp_path / "work"))
    return recorded


def test_a_cluster_is_made_loaded_and_removed(programs: Programs, tmp_path: Path) -> None:
    with kind.kind_cluster(tmp_path, ["shop:1"]) as cluster:
        assert cluster.gateway() == "172.18.0.1"
        cluster.claim("ddci-1", tmp_path / "world")
    commands = [" ".join(call[:3]) for call in programs.calls]
    assert commands[0] == "kind create cluster"
    assert "docker pull --quiet" in commands
    assert commands[-1] == "kind delete cluster"
    config = json.loads((tmp_path / "kind.yaml").read_text("utf-8"))
    mount = config["nodes"][0]["extraMounts"][0]
    assert mount == {"hostPath": str(tmp_path / "work"), "containerPath": str(tmp_path / "work")}
    applied = [call[-1] for call in programs.calls if call[0] == "kubectl"]
    assert '"hostPath": {"path": "' + str(tmp_path / "world") + '"}' in applied[0]
    assert '"volumeName": "ddci-1"' in applied[1]
    assert not list(tmp_path.glob("image-*.tar"))


def test_a_present_image_is_not_pulled(programs: Programs, tmp_path: Path) -> None:
    programs.images = "sha256:abc\n"
    kind.Cluster("ddci-1", tmp_path / "kubeconfig").load("shop:1", tmp_path)
    assert not [call for call in programs.calls if call[1] == "pull"]


def test_failures_name_the_command(programs: Programs, tmp_path: Path) -> None:
    programs.failing = {"kind", "kubectl"}
    with (
        pytest.raises(RuntimeError, match=r"^kind create failed: kind: broken$"),
        kind.kind_cluster(tmp_path, []),
    ):
        pass
    cluster = kind.Cluster("ddci-1", tmp_path / "kubeconfig")
    with pytest.raises(
        RuntimeError, match=r"^kubectl get pods --namespace failed: kubectl: broken"
    ):
        cluster.kubectl("get", "pods", "--namespace", "shop")
    assert cluster.kubectl("get", "pods", check=False) == ""


def test_waiting_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 1.0, 100.0, 200.0])

    def monotonic() -> float:
        return next(clock)

    def sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("datadouble_conformance.kind.time.monotonic", monotonic)
    monkeypatch.setattr("datadouble_conformance.kind.time.sleep", sleep)
    with pytest.raises(TimeoutError, match="timed out waiting for the pod"):
        kind.wait(lambda: False, "the pod", 10)
    kind.wait(lambda: True, "nothing", 10)
