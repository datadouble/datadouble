# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Signing golden images with cosign, and checking their signatures.

What is signed is the image summary without its signature field: it names
every archive's SHA-256, so the signature covers the image's contents. The
signature is a Sigstore bundle stored beside the summary. Keys are passed to
cosign by reference (``env://NAME`` or a path), so Datadouble never holds key
material.
"""

import json
import tempfile
from pathlib import Path
from typing import assert_never

from datadouble.images import ImageSummary
from datadouble.programs import locate, run_program
from datadouble.spec import CosignSigning, CosignTrust, EnvSecret, FileSecret, SecretRef


class SigningError(Exception):
    """A signature that could not be made or does not verify."""


def payload(summary: ImageSummary) -> bytes:
    """The bytes a signature covers: the summary without its signature, canonically."""
    data = summary.model_copy(update={"signature": None}).model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def bundle_key(summary: ImageSummary) -> str:
    """The store key of the signature bundle beside the summary."""
    return summary.identity.summary_key().replace(".summary.json", ".sigstore.json")


def _key(ref: SecretRef) -> str:
    match ref:
        case EnvSecret(env=name):
            return f"env://{name}"
        case FileSecret(file=path):
            return str(path)
        case _:
            assert_never(ref)


def cosign_binary() -> str:
    """``DATADOUBLE_COSIGN``, or ``cosign`` on ``PATH``."""
    found = locate("cosign", "DATADOUBLE_COSIGN")
    if not found:
        message = "cosign was not found; install it or set DATADOUBLE_COSIGN"
        raise SigningError(message)
    return found


def _cosign(arguments: list[str], action: str) -> None:
    done = run_program(cosign_binary(), arguments)
    if done.status != 0:
        message = f"{action} failed: {done.last_error()}"
        raise SigningError(message)


def sign(summary: ImageSummary, settings: CosignSigning) -> bytes:
    """Sign a summary; return the Sigstore bundle."""
    with tempfile.TemporaryDirectory(prefix="dd-sign-") as directory:
        data = Path(directory) / "payload.json"
        bundle = Path(directory) / "bundle.json"
        data.write_bytes(payload(summary))
        arguments = ["sign-blob", "--yes", f"--bundle={bundle}"]
        if settings.key is not None:
            arguments.append(f"--key={_key(settings.key)}")
        if not settings.transparency_log:
            arguments.extend(["--use-signing-config=false", "--tlog-upload=false"])
        _cosign([*arguments, str(data)], "signing the image")
        return bundle.read_bytes()


def verify(summary: ImageSummary, bundle: bytes, trust: CosignTrust) -> None:
    """Raise :class:`SigningError` unless ``bundle`` is a trusted signature of ``summary``."""
    with tempfile.TemporaryDirectory(prefix="dd-verify-") as directory:
        data = Path(directory) / "payload.json"
        signature = Path(directory) / "bundle.json"
        data.write_bytes(payload(summary))
        signature.write_bytes(bundle)
        arguments = ["verify-blob", f"--bundle={signature}"]
        if trust.public_key is not None:
            arguments.append(f"--key={_key(trust.public_key)}")
        else:
            arguments.extend(
                [
                    f"--certificate-identity={trust.identity}",
                    f"--certificate-oidc-issuer={trust.issuer}",
                ]
            )
        if not trust.transparency_log:
            arguments.append("--insecure-ignore-tlog=true")
        _cosign([*arguments, str(data)], "checking the image signature")
