# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite's own helpers."""

from collections.abc import Sequence

import pytest

from datadouble.programs import Completed
from datadouble_conformance import ci


def test_docker_failures_name_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(program: str, arguments: Sequence[str], **_: object) -> Completed:
        return Completed(status=1, stdout="", stderr=f"{program} {arguments[0]}: no such thing\n")

    monkeypatch.setattr(ci, "run_program", failing)
    with pytest.raises(RuntimeError, match=r"^docker inspect failed: docker inspect: no such"):
        ci.docker("inspect", "nothing")
