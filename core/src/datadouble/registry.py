# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Finding installed adapters through entry points.

Engines register under ``datadouble.engines``, stores under
``datadouble.stores``, container runtimes under ``datadouble.runtimes``,
replicate adapters under ``datadouble.replicators``, announce adapters
under ``datadouble.announcers`` and CI/CD adapters under ``datadouble.ci``, so third-party adapters
installed from anywhere are found the same way as Datadouble's own.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from pydantic import JsonValue

from datadouble.announce import AnnouncerFactory
from datadouble.ci import CiAdapter, CiFactory, RemoteSettings
from datadouble.engine import EngineAdapter
from datadouble.replicate import ReplicatorFactory
from datadouble.runtime import ContainerRuntime, RuntimeFactory
from datadouble.settings import remote_keys
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
    replicators: Mapping[str, ReplicatorFactory] = field(
        default_factory=dict[str, ReplicatorFactory]
    )
    announcers: Mapping[str, AnnouncerFactory] = field(default_factory=dict[str, AnnouncerFactory])
    runtimes: Mapping[str, RuntimeFactory] = field(default_factory=dict[str, RuntimeFactory])
    ci: Mapping[str, CiFactory] = field(default_factory=dict[str, CiFactory])

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

    def runtime(self, kind: str, settings: Mapping[str, JsonValue]) -> ContainerRuntime:
        """A container runtime of ``kind`` built from its settings."""
        factory = self.runtimes.get(kind)
        if factory is None:
            installed = ", ".join(sorted(self.runtimes)) or "none"
            message = f"no container runtime named {kind!r} is installed (installed: {installed})"
            raise RegistryError(message)
        return factory(settings)

    def ci_system(self, name: str) -> CiAdapter:
        """The CI/CD adapter registered as ``name``."""
        factory = self.ci.get(name)
        if factory is None:
            installed = ", ".join(sorted(self.ci)) or "none"
            message = (
                f"no CI/CD adapter named {name!r} is installed (installed: {installed});"
                f" install datadouble-{name}"
            )
            raise RegistryError(message)
        return factory()

    def remote_settings(self) -> RemoteSettings:
        """The settings each adapter declares remote, which ``ci init`` never mounts."""
        return RemoteSettings(
            engines={name: remote_keys(factory) for name, factory in self.engines.items()},
            stores={name: remote_keys(factory) for name, factory in self.stores.items()},
            replicators={name: remote_keys(factory) for name, factory in self.replicators.items()},
            announcers={name: remote_keys(factory) for name, factory in self.announcers.items()},
            runtimes={name: remote_keys(factory) for name, factory in self.runtimes.items()},
        )


def installed() -> Registry:
    """Every adapter installed in this environment."""
    engines: dict[str, EngineFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.engines")
    }
    stores: dict[str, StoreFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.stores")
    }
    replicators: dict[str, ReplicatorFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.replicators")
    }
    announcers: dict[str, AnnouncerFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.announcers")
    }
    runtimes: dict[str, RuntimeFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.runtimes")
    }
    ci: dict[str, CiFactory] = {
        point.name: point.load() for point in entry_points(group="datadouble.ci")
    }
    return Registry(
        engines=engines,
        stores=stores,
        replicators=replicators,
        announcers=announcers,
        runtimes=runtimes,
        ci=ci,
    )
