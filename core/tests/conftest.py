# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Fixtures shared by the core's tests."""

from pathlib import Path

import pytest

from datadouble.announce import ci_outputs_announcer, dotenv_announcer
from datadouble.registry import Registry
from datadouble.replicate import filesystem_replicator
from datadouble.storage import filesystem_store

FAKE_COSIGN = r"""#!/bin/sh
# Stands in for cosign: a "signature" is the payload's SHA-256 and the key name.
command=$1; shift
for argument; do
  case $argument in
    --bundle=*) bundle=${argument#--bundle=} ;;
    --key=*) key=${argument#--key=} ;;
    --*) ;;
    *) file=$argument ;;
  esac
done
sum=$(sha256sum "$file" | cut -d' ' -f1)
if [ "$command" = sign-blob ]; then
  printf '{"sha256": "%s"}' "$sum" > "$bundle"
  exit 0
fi
grep -q "$sum" "$bundle" || { echo "Error: invalid signature" >&2; exit 1; }
"""


@pytest.fixture(autouse=True)
def cosign(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path_factory.mktemp("bin") / "cosign"
    script.write_text(FAKE_COSIGN, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("DATADOUBLE_COSIGN", str(script))
    return script


@pytest.fixture
def registry() -> Registry:
    return Registry(
        engines={},
        stores={"filesystem": filesystem_store},
        replicators={"filesystem": filesystem_replicator},
        announcers={"dotenv": dotenv_announcer, "ci-outputs": ci_outputs_announcer},
    )
