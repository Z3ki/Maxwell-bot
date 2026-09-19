"""Tiny service locator. Plugins publish and consume named services.

This is not a general DI framework. It exists so a plugin can ask for
``memory`` or ``scheduler`` without reaching into MaxwellBot internals.
"""

from __future__ import annotations

from typing import Any, Callable

_MISSING = object()


class ServiceContainer:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._factories: dict[str, Callable[[], Any]] = {}
        self._owners: dict[str, str] = {}

    def register(self, name: str, service: Any, *, owner: str = "core") -> None:
        key = str(name).strip()
        if not key:
            raise ValueError("service name is required")
        existing = self._owners.get(key)
        if existing and existing != owner and key in self._services:
            raise ValueError(
                f"service {key!r} is already owned by plugin {existing!r}"
            )
        self._services[key] = service
        self._owners[key] = owner

    def factory(self, name: str, factory: Callable[[], Any], *, owner: str = "core") -> None:
        key = str(name).strip()
        self._factories[key] = factory
        self._owners[key] = owner

    def get(self, name: str, default: Any = _MISSING) -> Any:
        key = str(name)
        if key in self._services:
            return self._services[key]
        if key in self._factories:
            value = self._factories[key]()
            self._services[key] = value
            return value
        if default is _MISSING:
            raise KeyError(f"service {key!r} is not registered")
        return default

    def unregister_owner(self, owner: str) -> int:
        removed = 0
        for key, plugin in list(self._owners.items()):
            if plugin != owner:
                continue
            self._services.pop(key, None)
            self._factories.pop(key, None)
            self._owners.pop(key, None)
            removed += 1
        return removed

    def owned_by(self, owner: str) -> list[str]:
        return sorted(name for name, plugin in self._owners.items() if plugin == owner)

    def names(self) -> list[str]:
        return sorted(set(self._services) | set(self._factories))
