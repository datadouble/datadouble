# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Parsing adapter settings from the spec.

The spec passes adapter-specific settings (``sources.<name>.connection``, each entry of
``store``) through untouched; the adapter parses them here, at its boundary,
into its own strict model.

An adapter also declares which of its settings name paths that are not on the
machine running the job, such as an export path on an NFS server or a key
prefix in a bucket, with :func:`remote` on its entry point. ``ci init`` mounts
every other absolute path in the spec into the job, but never those
(decision 140).
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError


class SettingsError(Exception):
    """Adapter settings that do not validate."""


class NoSettings(BaseModel):
    """Settings an adapter does not take: any key is an error."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def parse_settings[M: BaseModel](model: type[M], settings: Mapping[str, JsonValue], what: str) -> M:
    """Validate ``settings`` against ``model``; ``what`` names them in errors."""
    try:
        return model.model_validate_json(json.dumps(dict(settings)))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in problem['loc']) or '(settings)'}: {problem['msg']}"
            for problem in error.errors(include_url=False)
        )
        message = f"{what} settings are invalid: {problems}"
        raise SettingsError(message) from None


type SettingKey = tuple[str, ...]
"""Where a setting is inside an adapter's settings, such as ``("nfs", "path")``."""


@dataclass(frozen=True, slots=True)
class Remote[**P, R]:
    """An adapter's entry point, with the settings that name paths off the job's machine."""

    factory: Callable[P, R]
    keys: tuple[SettingKey, ...]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Build the adapter, as the undecorated entry point does."""
        return self.factory(*args, **kwargs)


def remote[**P, R](*keys: str) -> Callable[[Callable[P, R]], Remote[P, R]]:
    """Declare the settings, as dotted keys such as ``nfs.path``, that name remote paths.

    A remote path is one the job never opens on its own machine: a path on
    another server, a key in a bucket, a path inside a secret store. Engine
    adapters give keys inside the source, such as ``connection.patterns``.
    A key names its whole value, so a list or mapping there is left out too.
    """
    declared = tuple(tuple(key.split(".")) for key in keys)

    def declare(factory: Callable[P, R]) -> Remote[P, R]:
        return Remote(factory, declared)

    return declare


def remote_keys(factory: object) -> tuple[SettingKey, ...]:
    """The settings an entry point declared remote with :func:`remote`, if any."""
    return factory.keys if isinstance(factory, Remote) else ()
