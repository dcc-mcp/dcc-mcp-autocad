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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..compat import host_verdict
from ..write_contract import WriteVerificationError
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
        self._host_version: Optional[str] = None

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

    def host_version(self) -> Optional[str]:
        """Return ``AutoCAD.Application.Version`` (an ACADVER string)."""
        if self._host_version is not None:
            return self._host_version
        try:
            version = getattr(self.application, "Version", None)
        except Exception:  # noqa: BLE001 - version must never break an operation
            return None
        self._host_version = str(version) if version else None
        return self._host_version

    @contextmanager
    def _open_document(self, path: str, settle: bool = True) -> Iterator[Any]:
        """Open a drawing and always close it.

        ``settle`` only matters after a save: a freshly closed DWG stays locked
        for a moment, so the *next* call is the one that sees the flush. Read
        paths skip it rather than pay 0.4s to serialise a no-op.
        """
        document = None
        try:
            document = com_retry(lambda: self._documents().Open(str(path)))
        except Exception as exc:  # noqa: BLE001 - COM raises non-standard types
            raise TransportError("AutoCAD could not open %s" % path) from exc
        try:
            yield document
        finally:
            if document is not None:
                try:
                    document.Close(False)
                finally:
                    if settle:
                        _settle()

    def _reopen_summary(self, path: str) -> DrawingSummary:
        """Re-open a saved DWG and describe what is actually on disk."""
        with self._open_document(path) as document:
            return self._summarize(document)

    def _read_back_layers(self, path: str) -> List[str]:
        """Re-open a saved DWG and list the layer names it actually contains."""
        with self._open_document(path) as document:
            return [document.Layers.Item(i).Name for i in range(document.Layers.Count)]

    @staticmethod
    def _contains_layer(layers: Sequence[str], name: str) -> bool:
        return any(str(existing).lower() == str(name).lower() for existing in layers)

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
            "version": self.host_version(),
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
        with self._open_document(path, settle=False) as document:
            return self._summarize(document)

    def create_drawing(self, output_path: str, template: Optional[str] = None) -> DrawingSummary:
        destination = Path(output_path).expanduser()
        try:
            if template:
                document = com_retry(lambda: self._documents().Add(str(template)))
            else:
                document = com_retry(self._documents().Add)
        except Exception as exc:  # noqa: BLE001 - COM raises non-standard types
            raise TransportError("AutoCAD could not create a drawing") from exc
        try:
            document.SaveAs(str(destination))
            written = self._summarize(document)
        finally:
            if document is not None:
                document.Close(False)
                _settle()

        if not destination.is_file():
            raise WriteVerificationError(
                tool="create_drawing",
                check="drawing_saved",
                expected={"path": str(destination), "exists": True},
                actual={"path": str(destination), "exists": False},
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"output_path": str(destination), "template": template},
                remediation=(
                    "AutoCAD raised no error but wrote no file; check the output directory "
                    "is writable and that a modal dialog is not blocking SaveAs."
                ),
            )
        # Read back from disk, not from the document we still had open: the
        # in-memory copy is identical whether or not the save reached the file.
        read_back = self._reopen_summary(destination)
        if read_back.entity_count != written.entity_count:
            raise WriteVerificationError(
                tool="create_drawing",
                check="entity_count",
                expected=written.entity_count,
                actual=read_back.entity_count,
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"output_path": str(destination), "template": template},
                remediation=(
                    "The drawing is on disk but does not match the template that was loaded; "
                    "the save was truncated."
                ),
            )
        return read_back

    def add_entities(
        self, path: str, entities: Sequence[Dict[str, Any]], layer: Optional[str] = None
    ) -> Dict[str, Any]:
        _, client = _com_module()
        pythoncom, _ = _com_module()
        source = Path(path).expanduser()

        with self._open_document(str(source)) as document:
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
            expected_after = modelspace.Count

        with self._open_document(str(source)) as document:
            modelspace = document.ModelSpace
            actual_after = modelspace.Count
            assigned: List[str] = []
            if layer:
                # The appended entities are the tail of model space; read their
                # layers back so "created on the wrong layer" cannot pass.
                start = max(0, modelspace.Count - handled)
                assigned = [str(modelspace.Item(i).Layer) for i in range(start, modelspace.Count)]

        if actual_after != expected_after:
            raise WriteVerificationError(
                tool="add_entities",
                check="entity_count_persisted",
                expected=expected_after,
                actual=actual_after,
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "requested": len(entities), "layer": layer},
                remediation=(
                    "The entities were added in the session but %d of them did not survive "
                    "the save; check the DWG is not read-only or locked by another session."
                    % (expected_after - actual_after)
                ),
            )
        if layer:
            wrong = [name for name in assigned if name.lower() != str(layer).lower()]
            if wrong:
                raise WriteVerificationError(
                    tool="add_entities",
                    check="entity_layer_persisted",
                    expected=[str(layer)] * len(assigned),
                    actual=assigned,
                    host_version=self.host_version(),
                    host_matrix=host_verdict(self.host_version()),
                    params={"path": str(source), "layer": layer},
                    remediation=(
                        "The entities were saved but not on the requested layer; in AutoCAD a "
                        "layer must exist before an entity can be assigned to it."
                    ),
                )
        return {
            "path": str(source),
            "entities_added": handled,
            "entity_types": created,
            "entity_count_before": before,
            "entity_count_after": actual_after,
            "verified": True,
        }

    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        source = Path(path).expanduser()
        requested = list(add)

        with self._open_document(str(source)) as document:
            created = [name for name in requested if self._ensure_layer(document, name)]
            document.Save()

        layers = self._read_back_layers(source)
        missing = [name for name in requested if not self._contains_layer(layers, name)]
        if missing:
            raise WriteVerificationError(
                tool="manage_layers",
                check="layers_persisted",
                expected=missing,
                actual=layers,
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "add": requested},
                remediation=(
                    "Layer creation returned success but the names are absent after reload; "
                    "the save did not reach the file."
                ),
            )
        return {"path": str(source), "created": created, "layers": layers, "verified": True}

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
