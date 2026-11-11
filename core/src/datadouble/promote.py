# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""``promote``: the only way an image leaves production.

``promote`` runs in production. It copies images from the build's store into
the export stores, the only storage non-production can read, and advances a
channel there. It checks every image before copying any, and refuses an image
unless:

- verification passed, with every check run and none failed;
- it was built with a strict-mode level at least as strict as required;
- it carries a signature the spec's ``trust`` settings accept;
- every archive's content matches the hash in its summary.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from datadouble.channels import point_channel
from datadouble.images import ImageSummary, digest, generations, image_lines, read_summary
from datadouble.names import Generation, GroupName
from datadouble.signing import SigningError, bundle_key, verify
from datadouble.spec import CosignTrust, Spec, StrictLevel
from datadouble.storage import Store

if TYPE_CHECKING:
    from datadouble.registry import Registry

_STRICTNESS = {StrictLevel.OFF: 0, StrictLevel.TABLES: 1, StrictLevel.COLUMNS: 2}


class PromoteError(Exception):
    """Images that may not cross into non-production, and why."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        """Keep every reason."""
        count = len(problems)
        super().__init__(f"promote refused: {count} problem{'s' if count != 1 else ''}")
        self.problems = problems


class Promoted(BaseModel):
    """One image now in the export stores."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    summary: ImageSummary
    channel: str
    stores: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    prefix: str
    summary: ImageSummary
    bundle: bytes
    files: dict[str, Path]


def check_signature(store: Store, summary: ImageSummary, trust: CosignTrust) -> bytes:
    """The image's signature bundle, verified; raises :class:`SigningError`."""
    if summary.signature is None:
        message = "the image is not signed"
        raise SigningError(message)
    bundle = store.get_bytes(summary.signature)
    if bundle is None:
        message = f"its signature {summary.signature} is missing"
        raise SigningError(message)
    verify(summary, bundle, trust)
    return bundle


def _problems(summary: ImageSummary, required: StrictLevel) -> list[str]:
    name = f"{summary.identity.prefix()}/{summary.identity.generation}"
    problems: list[str] = []
    verification = summary.verification
    if not verification.passed or verification.failed or not verification.checks:
        problems.append(f"{name}: verification did not pass")
    if _STRICTNESS[summary.strict] < _STRICTNESS[required]:
        problems.append(
            f"{name}: built with strict mode {summary.strict}, but {required} is required"
        )
    return problems


class _Promotion:
    def __init__(self, spec: Spec, registry: "Registry", scratch: Path) -> None:
        if spec.promote is None or spec.trust is None:
            message = "the spec needs `promote` and `trust` sections to promote images"
            raise PromoteError((message,))
        if not spec.store:
            raise PromoteError(("the spec lists no stores to promote from",))
        self.spec = spec
        self.settings = spec.promote
        self.trust = spec.trust.cosign
        self.source = registry.store(*next(iter(spec.store[0].items())))
        self.targets = [
            registry.store(kind, settings)
            for entry in spec.promote.to
            for kind, settings in entry.items()
        ]
        self.scratch = scratch

    def candidate(self, prefix: str, generation: Generation | None) -> _Candidate | str | None:
        """The image to promote from one line, the reasons it may not be, or None."""
        known = generations(self.source, prefix)
        chosen = generation or known[-1]
        summary = read_summary(self.source, prefix, chosen) if chosen in known else None
        if summary is None:
            return None
        problems = _problems(summary, self.settings.strict)
        try:
            bundle = check_signature(self.source, summary, self.trust)
        except SigningError as error:
            problems.append(f"{prefix}/{summary.identity.generation}: {error}")
            bundle = b""
        files: dict[str, Path] = {}
        for archive in summary.archives:
            local = self.scratch / archive.key.replace("/", "_")
            if not self.source.get_file(archive.key, local):
                problems.append(f"{archive.key} is missing")
            elif digest(local) != archive.digest:
                problems.append(f"{archive.key} does not match the hash in its summary")
            files[archive.key] = local
        return "; ".join(problems) if problems else _Candidate(prefix, summary, bundle, files)

    def copy(self, candidate: _Candidate) -> Promoted:
        summary = candidate.summary
        report_key = summary.identity.summary_key().replace(".summary.json", ".verify.json")
        report = self.source.get_bytes(report_key)
        for target in self.targets:
            for key, local in candidate.files.items():
                target.put_file(key, local)
            if report is not None:
                target.put_bytes(report_key, report)
            target.put_bytes(bundle_key(summary), candidate.bundle)
            target.put_bytes(
                summary.identity.summary_key(), summary.model_dump_json(indent=2).encode()
            )
            point_channel(
                target, candidate.prefix, self.settings.channel, summary.identity.generation
            )
        return Promoted(
            summary=summary,
            channel=self.settings.channel,
            stores=tuple(target.describe() for target in self.targets),
        )


def promote(
    spec: Spec,
    registry: "Registry",
    *,
    groups: tuple[GroupName, ...] = (),
    generation: Generation | None = None,
) -> tuple[Promoted, ...]:
    """Promote the newest (or the given) generation of each group; all or nothing."""
    with tempfile.TemporaryDirectory(prefix="dd-promote-") as directory:
        promotion = _Promotion(spec, registry, Path(directory))
        wanted = groups or tuple(spec.images)
        candidates: list[_Candidate] = []
        problems: list[str] = []
        for group in wanted:
            prefixes = image_lines(promotion.source, spec.name, group)
            if not prefixes:
                problems.append(f"{spec.name}/{group}: no images in {promotion.source.describe()}")
            found = [promotion.candidate(prefix, generation) for prefix in prefixes]
            problems.extend(item for item in found if isinstance(item, str))
            candidates.extend(item for item in found if isinstance(item, _Candidate))
            if prefixes and generation is not None and all(item is None for item in found):
                problems.append(f"{spec.name}/{group}: no generation {generation}")
        if problems:
            raise PromoteError(tuple(problems))
        return tuple(promotion.copy(candidate) for candidate in candidates)
