# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Finding installed adapters through entry points.

Engines register under ``datadouble.engines`` and stores under
``datadouble.stores``, so third-party adapters installed from anywhere are
found the same way as Datadouble's own.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points

from pydantic import JsonValue

from datadouble.engine import EngineAdapter
from datadouble.storage import Store

type EngineFactory = Callable[[], EngineAdapter]
type StoreFactory = Callable[[Mapping[str, JsonValue]], Store]


class RegistryError(Exception):
    """An adapter that is not installed."""


@dataclass(frozen=True, slots=True)
class Registry:
    """The adapters available to a run."""

    engines: Mapping[str, EngineFactory]
    stores: Mapping[str, StoreFactory]

    def engine(self, name: str) -> EngineAdapter:
        """The engine adapter registered as ``name``."""
        factory = self.engines.get(name)
        if factory is None:
            installed = ", ".join(sorted(self.engines)) or "none"
            message = (
                f"no engine adapter named {name!r} is installed (installed: {installed}); "
                f"install datadouble-{name}"
            )
            raise RegistryError(message)
        return factory()

    def store(self, kind: str, settings: Mapping[str, JsonValue]) -> Store:
        """A store of ``kind`` built from its settings."""
        factory = self.stores.get(kind)
        if factory is None:
            installed = ", ".join(sorted(self.stores)) or "none"
            message = f"no store adapter named {kind!r} is installed (installed: {installed})"
            raise RegistryError(message)
        return factory(settings)


def installed() -> Registry:
    """Every adapter installed in this environment."""
    engines: dict[str, EngineFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.engines")
    }
    stores: dict[str, StoreFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.stores")
    }
    return Registry(engines=engines, stores=stores)
