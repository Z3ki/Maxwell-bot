"""Owner-scoped publication into the legacy process-wide schema catalog.

The live registry remains canonical. This compatibility layer refreshes only
schemas added by plugin managers, preserves the catalog's built-in schemas,
and restores another live manager's contribution when an owner is unloaded.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable
import weakref

from .spec import ToolSpec


@dataclass
class _Contribution:
    schemas: dict[str, dict[str, Any]]
    result_names: set[str]


class _PublicationState:
    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        self.parameters = catalog.TOOL_PARAMETERS
        self.owners: OrderedDict[weakref.ReferenceType, _Contribution] = OrderedDict()
        self.written: dict[str, dict[str, Any]] = {}

    def publish(self, owner: Any, specs: Iterable[ToolSpec]) -> None:
        tools = list(specs)
        reference = weakref.ref(owner, self._discard_owner)
        self.owners.pop(reference, None)
        self.owners[reference] = _Contribution(
            schemas={spec.name: spec.schema() for spec in tools},
            result_names={spec.name for spec in tools if spec.returns_result},
        )
        self._apply()

    def _discard_owner(self, reference: weakref.ReferenceType) -> None:
        self.owners.pop(reference, None)
        self._apply()

    def _apply(self) -> None:
        schemas: dict[str, dict[str, Any]] = {}
        result_names: set[str] = set()
        for contribution in self.owners.values():
            schemas.update(contribution.schemas)
            result_names.update(contribution.result_names)
        # If unrelated code replaced a managed entry, that value becomes an
        # external catalog entry. Do not delete or overwrite it on unload.
        for name, written in list(self.written.items()):
            if self.parameters.get(name) is not written:
                self.written.pop(name)
        for name in self.written.keys() - schemas.keys():
            self.parameters.pop(name, None)
            self.written.pop(name)
        for name, schema in schemas.items():
            if name not in self.parameters or name in self.written:
                self.parameters[name] = schema
                self.written[name] = schema
        self.catalog.set_plugin_result_tools(result_names)


def publish_plugin_tools(owner: Any, specs: Iterable[ToolSpec]) -> None:
    import tool_schemas

    state = getattr(tool_schemas, "_maxwell_plugin_publications", None)
    if state is None or state.parameters is not tool_schemas.TOOL_PARAMETERS:
        state = _PublicationState(tool_schemas)
        tool_schemas._maxwell_plugin_publications = state
    state.publish(owner, specs)
