"""Interactive AutoCAD transport over the vanilla out-of-process COM surface.

Boundary notes:
  * Only the vanilla ``AutoCAD.Application`` object model is used. No vertical
    (Architecture/Mechanical/Electrical) or OEM-specific API is referenced, so
    the adapter behaves identically on plain AutoCAD and on vertical builds.
  * The ProgID is an *accelerator*, never a prerequisite. Resolution walks an
    explicit-version -> CurVer -> latest-registered fallback chain so the
    adapter keeps working when a given seat registers a different version.
  * Late binding is used throughout: the makepy ``gen_py`` cache has been
    observed to be incomplete (``IAcadLine`` missing ``Color``).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence

from .base import (
    COM_CAPABILITIES,
    Capability,
    DrawingSummary,
    Transport,
    TransportError,
    TransportUnavailable,
)

# Tried in order. The first entry is the exact version verified during
# evaluation; the rest keep other seats working.
PROG_ID_CANDIDATES = (
    "AutoCAD.Application.25.1",
    "AutoCAD.Application.25",
    "AutoCAD.Application.24.3",
    "AutoCAD.Application",
)

_VARIANT_ARRAY_R8 = 0x2005  # VT_ARRAY | VT_R8

# AutoCAD answers COM with "call was rejected by callee" while it is busy
# (startup, a modal dialog, a regeneration). These are transient and safe to
# retry, unlike a genuine automation error.
_RETRYABLE_HRESULTS = frozenset(
    {
        -2147418111,  # RPC_E_CALL_REJECTED
        -2147417846,  # RPC_E_SERVERCALL_RETRYLATER
    }
)
# A freshly saved DWG stays locked for a couple of seconds, so the budget has
# to clear that window rather than just a couple of quick retries.
_DEFAULT_ATTEMPTS = 30
_DEFAULT_DELAY = 0.5

# After a save + close, AutoCAD keeps flushing the DWG briefly. A short settle
# before returning makes back-to-back operations reliable instead of relying
# purely on rejection retries.
_SETTLE_SECS = 0.4


def _com_module():
    """Import pywin32 lazily so non-Windows imports stay cheap."""
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise TransportUnavailable("pywin32 is required for the COM transport") from exc
    return pythoncom, win32com.client


def _settle(seconds: float = _SETTLE_SECS) -> None:
    """Give AutoCAD a moment to finish a save before the next COM call."""
    time.sleep(seconds)


def _hresult(exc: BaseException) -> Optional[int]:
    """Extract the COM HRESULT from the various shapes pywin32 raises."""
    for candidate in (getattr(exc, "hresult", None), getattr(exc, "args", None)):
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, (tuple, list)):
            for item in candidate:
                if isinstance(item, int) and item < 0:
                    return item
    return None


def com_retry(func, attempts: int = _DEFAULT_ATTEMPTS, delay: float = _DEFAULT_DELAY):
    """Retry a COM call while AutoCAD reports it is busy.

    Bounded and only for the documented transient HRESULTs, so a real error
    still surfaces immediately rather than being retried to death.
    """
    last: Optional[BaseException] = None
    for _ in range(max(1, int(attempts))):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - COM raises non-standard types
            if _hresult(exc) not in _RETRYABLE_HRESULTS:
                raise
            last = exc
            time.sleep(delay)
    raise last  # type: ignore[misc]


class ComTransport(Transport):
    """Drives a live AutoCAD session through COM."""

    name = "com"
    capabilities = COM_CAPABILITIES

    def __init__(self, prog_id: Optional[str] = None, visible: bool = True) -> None:
        self.prog_id = prog_id
        self.visible = bool(visible)
        self._app = None
        self._initialised = False
        self._resolved_prog_id: Optional[str] = None

    # -- plumbing ---------------------------------------------------------
    def _ensure_com(self) -> None:
        if self._initialised:
            return
        pythoncom, _ = _com_module()
        # COM is STA: the calling thread must be initialised before any call.
        pythoncom.CoInitialize()
        self._initialised = True

    def _resolve_prog_id(self) -> str:
        """Walk the fallback chain and return the first ProgID that binds."""
        _, client = _com_module()
        candidates = [self.prog_id] if self.prog_id else list(PROG_ID_CANDIDATES)
        errors: List[str] = []
        for candidate in candidates:
            try:
                # Late binding: avoid the makepy cache entirely.
                app = client.dynamic.Dispatch(candidate)
            except Exception as exc:  # noqa: BLE001 - COM raises non-standard types
                errors.append("%s: %s" % (candidate, exc))
                continue
            self._app = app
            return candidate
        raise TransportUnavailable("No AutoCAD COM ProgID could be bound (%s)" % "; ".join(errors))

    @property
    def application(self):
        if self._app is None:
            self._ensure_com()
            self._resolved_prog_id = com_retry(self._resolve_prog_id)
            try:
                self._app.Visible = self.visible
            except Exception:  # noqa: BLE001 - verticals may reject the setter
                pass
        return self._app

    def _documents(self):
        """Access the Documents collection with retry.

        Even reading ``app.Documents`` is rejected while AutoCAD is busy, so
        the property access itself must be inside the retry boundary.
        """
        return com_retry(lambda: self.application.Documents)

    def is_available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            _com_module()
        except TransportUnavailable:
            return False
        try:
            self._ensure_com()
            com_retry(self._resolve_prog_id)
        except Exception:  # noqa: BLE001 - COM raises non-standard types
            return False
        return True

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _point(client, pythoncom, values):
        """Build a 3D point variant.

        AutoCAD rejects 2-element coordinates with "incorrect number of
        elements in the safe array", so 2D input is zero-padded to Z=0.
        """
        coords = [float(v) for v in values]
        if len(coords) == 2:
            coords.append(0.0)
        if len(coords) != 3:
            raise TransportError("points must have 2 or 3 coordinates")
        return client.VARIANT(_VARIANT_ARRAY_R8, tuple(coords))

    def _active_document(self):
        app = self.application
        try:
            return app.ActiveDocument
        except Exception as exc:  # noqa: BLE001
            raise TransportError("No active AutoCAD document is available") from exc

    @staticmethod
    def _summarize(document, fallback_name: str = "") -> DrawingSummary:
        modelspace = document.ModelSpace
        counts: Dict[str, int] = {}
        for index in range(modelspace.Count):
            entity = modelspace.Item(index)
            counts[entity.EntityName] = counts.get(entity.EntityName, 0) + 1
        layers = [document.Layers.Item(i).Name for i in range(document.Layers.Count)]
        layouts = [document.Layouts.Item(i).Name for i in range(document.Layouts.Count)]
        name = fallback_name
        try:
            name = document.GetVariable("DWGNAME") or fallback_name
        except Exception:  # noqa: BLE001
            pass
        path = None
        try:
            full = document.GetVariable("DWGPREFIX")
            if full and name:
                path = "%s%s" % (full, name)
        except Exception:  # noqa: BLE001
            pass
        return DrawingSummary(
            name=name,
            path=path,
            entity_count=modelspace.Count,
            entity_types=counts,
            layers=layers,
            layouts=layouts,
        )

    # -- transport surface -------------------------------------------------
    def status(self) -> Dict[str, Any]:
        app = self.application
        document = None
        try:
            document = app.ActiveDocument
        except Exception:  # noqa: BLE001
            pass
        info: Dict[str, Any] = {
            "transport": self.name,
            "ready": True,
            "prog_id": self._resolved_prog_id,
            "version": getattr(app, "Version", None),
            "capabilities": sorted(c.value for c in self.capabilities),
        }
        if document is not None:
            info["document"] = self._summarize(document).as_dict()
        try:
            info["hwnd"] = int(app.HWND)
        except Exception:  # noqa: BLE001
            info["hwnd"] = None
        return info

    def inspect_drawing(self, path: str, max_entities: int = 1000) -> DrawingSummary:
        try:
            document = com_retry(lambda: self._documents().Open(str(path)))
        except Exception as exc:  # noqa: BLE001
            raise TransportError("AutoCAD could not open %s" % path) from exc
        try:
            return self._summarize(document)
        finally:
            document.Close(False)

    def create_drawing(self, output_path: str, template: Optional[str] = None) -> DrawingSummary:
        try:
            if template:
                document = com_retry(lambda: self._documents().Add(str(template)))
            else:
                document = com_retry(self._documents().Add)
        except Exception as exc:  # noqa: BLE001
            raise TransportError("AutoCAD could not create a drawing") from exc
        try:
            document.SaveAs(str(output_path))
            return self._summarize(document)
        finally:
            document.Close(False)

    def add_entities(
        self, path: str, entities: Sequence[Dict[str, Any]], layer: Optional[str] = None
    ) -> Dict[str, Any]:
        _, client = _com_module()
        pythoncom, _ = _com_module()
        document = None
        try:
            document = com_retry(lambda: self._documents().Open(str(path)))
        except Exception as exc:  # noqa: BLE001
            raise TransportError("AutoCAD could not open %s" % path) from exc

        try:
            if layer:
                self._ensure_layer(document, layer)
            modelspace = document.ModelSpace
            before = modelspace.Count
            created: List[str] = []
            handled = 0
            for index, entity in enumerate(entities):
                kind = str(entity.get("type", "")).lower()
                if kind == "line":
                    modelspace.AddLine(
                        self._point(client, pythoncom, entity["start"]),
                        self._point(client, pythoncom, entity["end"]),
                    )
                elif kind == "circle":
                    modelspace.AddCircle(
                        self._point(client, pythoncom, entity["center"]),
                        float(entity["radius"]),
                    )
                elif kind == "point":
                    modelspace.AddPoint(self._point(client, pythoncom, entity["position"]))
                elif kind == "text":
                    modelspace.AddText(
                        str(entity["text"]),
                        self._point(client, pythoncom, entity["position"]),
                        float(entity.get("height", 2.5)),
                    )
                else:
                    raise TransportError("entities[%d].type is unsupported: %s" % (index, kind))
                created.append(kind)
                handled += 1
                if layer:
                    modelspace.Item(modelspace.Count - 1).Layer = layer
            document.Save()
            return {
                "path": str(path),
                "entities_added": handled,
                "entity_types": created,
                "entity_count_before": before,
                "entity_count_after": modelspace.Count,
            }
        finally:
            if document is not None:
                document.Close(False)
                _settle()

    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        document = None
        try:
            document = com_retry(lambda: self._documents().Open(str(path)))
        except Exception as exc:  # noqa: BLE001
            raise TransportError("AutoCAD could not open %s" % path) from exc
        try:
            created = []
            for name in add:
                if self._ensure_layer(document, name):
                    created.append(name)
            document.Save()
            return {
                "path": str(path),
                "created": created,
                "layers": [document.Layers.Item(i).Name for i in range(document.Layers.Count)],
            }
        finally:
            if document is not None:
                document.Close(False)
                _settle()

    @staticmethod
    def _ensure_layer(document, name: str) -> bool:
        """Create a layer when missing; return True when it was created.

        AutoCAD rejects assigning an entity to a layer that does not exist, so
        this must run before any entity is placed on the layer.
        """
        for index in range(document.Layers.Count):
            if document.Layers.Item(index).Name.lower() == name.lower():
                return False
        document.Layers.Add(name)
        return True

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def close(self) -> None:
        self._app = None

    def __del__(self):  # pragma: no cover - best effort COM cleanup
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def _timestamp(self) -> float:
        return time.time()
