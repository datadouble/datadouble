# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Resolving secret references.

Specs and connection settings name secrets by reference. Resolved values are
handed to whatever needs them and never written to manifests, artifacts or
logs, and each is passed to the CI system's log masking when a request names
one (:func:`datadouble.ci.mask`).
"""

import os
from collections.abc import Mapping
from typing import assert_never

from datadouble.ci import mask
from datadouble.spec import EnvSecret, FileSecret, SecretRef


class SecretError(Exception):
    """A secret reference that cannot be resolved."""


def resolve_secret(ref: SecretRef, environ: Mapping[str, str] | None = None) -> str:
    """The value a reference points at, without trailing newlines from files."""
    value = _resolve(ref, environ)
    mask(value)
    return value


def _resolve(ref: SecretRef, environ: Mapping[str, str] | None) -> str:
    environ = os.environ if environ is None else environ
    match ref:
        case EnvSecret(env=name):
            value = environ.get(name)
            if not value:
                message = f"environment variable {name} is not set"
                raise SecretError(message)
            return value
        case FileSecret(file=path):
            try:
                return path.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as error:
                message = f"cannot read secret file {path}: {error.strerror}"
                raise SecretError(message) from None
        case _:
            assert_never(ref)
