"""Deterministic prompt composition.

Plugins register components. The host asks the manager for the slices that
apply to this turn. Disabled plugins contribute nothing. Tool-specific
instructions are included only when that tool is actually offered.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .component import POSITIONS, PromptComponent, PromptRequest

# Rough chars-per-token for budget clipping. Conservative on purpose.
_CHARS_PER_TOKEN = 4


class PromptManager:
    def __init__(self) -> None:
        self._components: dict[str, PromptComponent] = {}
        self._by_plugin: dict[str, set[str]] = defaultdict(set)

    def register(self, component: PromptComponent) -> None:
        cid = str(component.id or "").strip()
        if not cid:
            raise ValueError("prompt component id is required")
        if component.position not in POSITIONS:
            raise ValueError(
                f"unknown prompt position {component.position!r}. "
                f"Known: {', '.join(POSITIONS)}"
            )
        existing = self._components.get(cid)
        if existing is not None and existing.plugin != component.plugin:
            raise ValueError(
                f"prompt component {cid!r} is already registered by "
                f"{existing.plugin!r}"
            )
        self._components[cid] = component
        self._by_plugin[component.plugin].add(cid)

    def unregister_plugin(self, plugin: str) -> int:
        ids = list(self._by_plugin.pop(str(plugin), set()))
        for cid in ids:
            self._components.pop(cid, None)
        return len(ids)

    def plugin_components(self, plugin: str) -> list[str]:
        return sorted(self._by_plugin.get(str(plugin), set()))

    def components(self) -> list[PromptComponent]:
        return list(self._components.values())

    def assemble(
        self,
        request: PromptRequest,
        *,
        enabled_plugins: Iterable[str] | None = None,
    ) -> str:
        """Join applicable components in stable position/priority/id order."""
        parts = self.collect(request, enabled_plugins=enabled_plugins)
        return "\n\n".join(part for part in parts if part)

    def collect(
        self,
        request: PromptRequest,
        *,
        enabled_plugins: Iterable[str] | None = None,
    ) -> list[str]:
        allowed = None if enabled_plugins is None else set(enabled_plugins)
        selected: list[PromptComponent] = []
        for component in self._components.values():
            if allowed is not None and component.plugin not in allowed and component.plugin != "core":
                continue
            if not component.applies(request):
                continue
            selected.append(component)
        selected.sort(key=lambda c: (POSITIONS.index(c.position), c.priority, c.id))
        out: list[str] = []
        for component in selected:
            body = component.body(request)
            if not body:
                continue
            budget = component.token_budget
            if budget is not None and budget > 0:
                max_chars = int(budget) * _CHARS_PER_TOKEN
                if len(body) > max_chars:
                    body = body[: max_chars - 1].rstrip() + "…"
            out.append(body)
        return out

    def personality_text(
        self,
        request: PromptRequest,
        *,
        enabled_plugins: Iterable[str] | None = None,
        base: str = "",
    ) -> str:
        """Compose the personality slice plus any style components."""
        allowed = None if enabled_plugins is None else set(enabled_plugins)
        chunks: list[str] = []
        if base.strip():
            chunks.append(base.strip())
        style: list[PromptComponent] = []
        for component in self._components.values():
            if component.position not in {"personality", "style"}:
                continue
            if allowed is not None and component.plugin not in allowed and component.plugin != "core":
                continue
            if not component.applies(request):
                continue
            style.append(component)
        style.sort(key=lambda c: (c.priority, c.id))
        for component in style:
            body = component.body(request)
            if body and body not in chunks:
                chunks.append(body)
        return "\n\n".join(chunks)
