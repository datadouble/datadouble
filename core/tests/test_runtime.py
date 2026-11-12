# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import os
from pathlib import Path

import pytest

from datadouble.registry import Registry, RegistryError
from datadouble.runtime import Completed, ContainerError, absolute, check, identity
from fake_engine import FakeRuntime


def test_check() -> None:
    done = Completed(status=0, stdout="ok", stderr="")
    assert check(done, "x") is done
    with pytest.raises(ContainerError, match="dumping failed: last"):
        check(Completed(status=1, stdout="", stderr="first\nlast\n"), "dumping")
    with pytest.raises(ContainerError, match="dumping failed: status 3"):
        check(Completed(status=3, stdout="", stderr=""), "dumping")


def test_paths_and_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert absolute([Path("a")]) == (tmp_path.resolve() / "a",)
    assert identity() == f"{os.getuid()}:{os.getgid()}"


def test_registry_runtimes() -> None:
    runtime = FakeRuntime()
    registry = Registry(engines={}, stores={}, runtimes={"fake": lambda _settings: runtime})
    assert registry.runtime("fake", {}) is runtime
    with pytest.raises(RegistryError, match=r"no container runtime named 'docker'.*fake"):
        registry.runtime("docker", {})
    with pytest.raises(RegistryError, match=r"\(installed: none\)"):
        Registry(engines={}, stores={}).runtime("docker", {})
