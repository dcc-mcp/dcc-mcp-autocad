"""Transport negotiation and capability facade.

The bridge picks the richest transport the current host actually supports and
reports the resulting capability set explicitly. When only the headless
transport is usable, the missing interactive capabilities are enumerated as a
declared degradation rather than silently failing at call time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .discovery import HostPaths, discover
from .transports import (
    COM_CAPABILITIES,
    CORE_CONSOLE_CAPABILITIES,
    Capability,
    ComTransport,
    CoreConsoleTransport,
    DrawingSummary,
    Transport,
    TransportError,
    TransportUnavailable,
)

#: Preference order. COM first because it is the only fully capable transport.
_TRANSPORT_ORDER = ("com", "accoreconsole")


def _build_transports(
    gui_exe: Optional[str],
    core_console_exe: Optional[str],
    prefer: Optional[str] = None,
) -> List[Transport]:
    transports: List[Transport] = [ComTransport(), CoreConsoleTransport(core_console_exe)]
    order = list(_TRANSPORT_ORDER)
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    transports.sort(key=lambda t: order.index(t.name) if t.name in order else len(order))
    return transports


class AutoCadBridge:
    """Resolves one usable transport and exposes its capability set."""

    def __init__(
        self,
        gui_exe: Optional[str] = None,
        core_console_exe: Optional[str] = None,
        prefer: Optional[str] = None,
        force_transport: Optional[str] = None,
    ) -> None:
        self.paths = discover(gui_exe)
        if core_console_exe:
            self.paths = HostPaths(
                gui_exe=self.paths.gui_exe,
                core_console_exe=Path(core_console_exe).expanduser(),
                sources=list(self.paths.sources) + ["explicit_core_console"],
            )
        self._candidates = _build_transports(gui_exe, core_console_exe, prefer)
        self._forced = force_transport
        self._active: Optional[Transport] = None
        self._failures: Dict[str, str] = {}

    # -- negotiation -------------------------------------------------------
    def _select(self) -> Transport:
        if self._active is not None:
            return self._active
        forced = self._forced
        for transport in self._candidates:
            if forced and transport.name != forced:
                continue
            try:
                if transport.is_available():
                    self._active = transport
                    return transport
            except Exception as exc:  # noqa: BLE001 - transports raise many types
                self._failures[transport.name] = str(exc)
                continue
            self._failures.setdefault(transport.name, "unavailable")
        raise TransportUnavailable(
            "No AutoCAD transport is available (checked: %s)"
            % ", ".join(t.name for t in self._candidates)
        )

    @property
    def transport(self) -> Transport:
        return self._select()

    @staticmethod
    def _degradations(active: Transport) -> List[str]:
        """Enumerate interactive capabilities the active transport cannot offer."""
        missing = COM_CAPABILITIES - active.capabilities
        notes: List[str] = []
        for capability in sorted(missing, key=lambda c: c.value):
            if capability is Capability.LIVE_DOCUMENT:
                notes.append(
                    "no live document session: drawings are opened and saved per operation"
                )
            elif capability is Capability.HWND:
                notes.append("no window handle: UI targeting and window actions are unavailable")
            elif capability is Capability.INTERACTIVE_SELECTION:
                notes.append("no interactive selection: use typed entity arguments instead")
        return notes

    def capabilities(self) -> List[str]:
        return sorted(c.value for c in self.transport.capabilities)

    def degraded(self) -> List[str]:
        return self._degradations(self.transport)

    # -- status ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "ready": False,
            "platform": os.name,
            "discovery": self.paths.as_dict(),
            "checked_transports": [t.name for t in self._candidates],
        }
        try:
            active = self.transport
        except TransportUnavailable as exc:
            report.update(
                {
                    "reason": "no_transport_available",
                    "detail": str(exc),
                    "transport_failures": self._failures,
                    "capabilities": [],
                    "degraded": sorted(
                        "no %s" % c.value for c in COM_CAPABILITIES - CORE_CONSOLE_CAPABILITIES
                    ),
                }
            )
            return report
        try:
            report["runtime"] = active.status()
        except TransportError as exc:
            report.update(
                {
                    "reason": "transport_status_failed",
                    "detail": str(exc),
                    "transport": active.name,
                }
            )
            return report
        report.update(
            {
                "ready": True,
                "transport": active.name,
                "capabilities": self.capabilities(),
                "degraded": self.degraded(),
                "degraded_mode": active.name != "com",
            }
        )
        return report

    # -- operations --------------------------------------------------------
    def inspect_drawing(self, path: str, max_entities: int = 1000) -> DrawingSummary:
        return self.transport.inspect_drawing(path, max_entities)

    def create_drawing(self, output_path: str, template: Optional[str] = None) -> DrawingSummary:
        return self.transport.create_drawing(output_path, template)

    def add_entities(
        self, path: str, entities: Sequence[Dict[str, Any]], layer: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.transport.add_entities(path, entities, layer)

    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        return self.transport.manage_layers(path, add)


def get_bridge(
    gui_exe: Optional[str] = None,
    core_console_exe: Optional[str] = None,
    prefer: Optional[str] = None,
) -> AutoCadBridge:
    return AutoCadBridge(gui_exe=gui_exe, core_console_exe=core_console_exe, prefer=prefer)
