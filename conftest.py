# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Hooks for every test run: ``--shard`` (scripts/shard.py), and the boundary mark."""

import pytest
from shard import pytest_addoption, pytest_collection_modifyitems, pytest_configure

__all__ = ["pytest_addoption", "pytest_collection_modifyitems", "pytest_configure"]


@pytest.fixture(autouse=True)
def inside_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests read only their own fixtures, so every test runs marked as inside (decision 98).

    The unit tests run on GitHub's hosted runners, which say so in
    ``RUNNER_ENVIRONMENT``; tests of the hosted-runner refusal set it themselves.
    """
    monkeypatch.setenv("DATADOUBLE_BOUNDARY", "inside")
    monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
