"""Pluggable transport contract shared by every AutoCAD host path.

Both the interactive COM transport and the headless ``accoreconsole.exe``
transport implement this interface, so callers depend on capabilities rather
than on a hard-coded mechanism.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


class Capability(str, Enum):
    """Host capabilities a transport may provide."""

    #: Attach to a live, already-open drawing session.
    LIVE_DOCUMENT = "live_document"
    #: Expose the host window handle for UI Control / window targeting.
    HWND = "hwnd"
    #: Resolve interactive pick-set selections.
    INTERACTIVE_SELECTION = "interactive_selection"
    #: Open, mutate, and save drawings in batch.
    BATCH_EDIT = "batch_edit"
    #: Publish/plot drawings to PDF or a device.
    PLOT = "plot"


#: Capabilities the interactive COM transport can offer.
COM_CAPABILITIES = frozenset(
    {
        Capability.LIVE_DOCUMENT,
        Capability.HWND,
        Capability.INTERACTIVE_SELECTION,
        Capability.BATCH_EDIT,
        Capability.PLOT,
    }
)

#: Capabilities the headless core-console transport can offer. The omitted ones
#: are exactly the interactive-only features that degrade without COM.
CORE_CONSOLE_CAPABILITIES = frozenset({Capability.BATCH_EDIT, Capability.PLOT})


@dataclass(frozen=True)
class DrawingSummary:
    """Bounded, JSON-safe description of one drawing."""

    name: str
    path: Optional[str]
    entity_count: int
    entity_types: Dict[str, int] = field(default_factory=dict)
    layers: List[str] = field(default_factory=list)
    layouts: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "entity_count": self.entity_count,
            "entity_types": dict(self.entity_types),
            "layers": list(self.layers),
            "layouts": list(self.layouts),
        }


class TransportError(RuntimeError):
    """A bounded, user-actionable transport failure."""


class TransportUnavailable(TransportError):
    """The transport's host prerequisites are not satisfied."""


class Transport(ABC):
    """One concrete way of driving AutoCAD."""

    name: str = "abstract"
    capabilities: frozenset = frozenset()

    @abstractmethod
    def is_available(self) -> bool:
        """Return True when this transport can be used right now."""

    @abstractmethod
    def status(self) -> Dict[str, Any]:
        """Return a bounded readiness description."""

    @abstractmethod
    def inspect_drawing(self, path: str, max_entities: int = 1000) -> DrawingSummary:
        """Read a drawing and return bounded metadata."""

    @abstractmethod
    def create_drawing(self, output_path: str, template: Optional[str] = None) -> DrawingSummary:
        """Create a new drawing and return its summary."""

    @abstractmethod
    def add_entities(
        self, path: str, entities: Sequence[Dict[str, Any]], layer: Optional[str] = None
    ) -> Dict[str, Any]:
        """Append typed entities to a drawing and persist it."""

    @abstractmethod
    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        """Ensure layers exist on a drawing and report the resulting layer list."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities
