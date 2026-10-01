"""Headless AutoCAD transport driven through ``accoreconsole.exe`` over stdin.

This is the no-COM fallback that makes portable installs usable. It never
touches COM or the registry ProgID, only the resolved executable path.

Hard constraint discovered during evaluation: ``accoreconsole.exe /s <script>``
does **not** consume the script on AutoCAD 2026 (it prints usage and blocks).
The only reliable way to drive it is piping a CRLF-terminated script through
stdin, which exits cleanly and produces valid DWG output.

Capability note: this transport deliberately omits ``LIVE_DOCUMENT``, ``HWND``,
and ``INTERACTIVE_SELECTION``. Those require an interactive session and are
reported as degraded rather than silently failing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..compat import host_verdict
from ..discovery import discover
from ..write_contract import WriteVerificationError
from .base import (
    CORE_CONSOLE_CAPABILITIES,
    Capability,
    DrawingSummary,
    Transport,
    TransportError,
    TransportUnavailable,
)

#: AutoCAD scripts require CRLF line endings.
_LINE_ENDING = "\r\n"

# ACADVER is a string system variable. The type guard keeps `strcat` from
# erroring on a host that ever returns something else, which would collapse the
# whole probe instead of just this one field.
_ACADVER_EXPR = '(if (= (type (getvar "ACADVER")) (quote STR)) (getvar "ACADVER") "")'

# Written as literals rather than assembled by a helper: this is the only place
# in the adapter where Python, JSON, and AutoLISP quoting all meet, and the
# escaping is easier to read than to generate. Each line emits one JSON field
# whose value is the LISP expression spliced into `strcat`.
_STATUS_LINES = (
    '(write-line "{" dccfp)',
    '(write-line (strcat "\\"version\\": \\"" ' + _ACADVER_EXPR + ' "\\",") dccfp)',
    '(write-line (strcat "\\"acadver\\": \\"" ' + _ACADVER_EXPR + ' "\\"") dccfp)',
    '(write-line "}" dccfp)',
)

# Walk the layer symbol table. `tblnext` is vanilla AutoLISP, so this needs no
# Visual LISP / ActiveX surface that a portable install might not load.
_LAYER_WALK_LINES = (
    '(setq dcclayers "")',
    '(setq dccitem (tblnext "LAYER" T))',
    "(while dccitem",
    '  (setq dcclayers (strcat dcclayers (cdr (assoc 2 dccitem)) ","))',
    '  (setq dccitem (tblnext "LAYER"))',
    ")",
)

# Emitted after a mutation, against a freshly re-opened DWG, to prove the
# change survived the save rather than merely the session.
#
# Beside each entity type (group code 0) the walk also records the entity's
# layer (group code 8). A layer existing in the LAYER table says nothing about
# whether any given entity was assigned to it, so per-entity layers must be
# read out of the entities themselves, not from the table.
_READBACK_LINES = (
    (
        '(setq dccss (ssget "X"))',
        "(setq dccn (if dccss (sslength dccss) 0))",
        '(setq dcctypes "")',
        '(setq dcclayersof "")',
        "(setq dcci 0)",
        "(while (< dcci dccn)",
        "  (setq dcent (entget (ssname dccss dcci)))",
        '  (setq dcctypes (strcat dcctypes (cdr (assoc 0 dcent)) ","))',
        '  (setq dcclayersof (strcat dcclayersof (cdr (assoc 8 dcent)) ","))',
        "  (setq dcci (1+ dcci))",
        ")",
    )
    + _LAYER_WALK_LINES
    + (
        '(write-line "{" dccfp)',
        '(write-line (strcat "\\"entity_count\\": " (itoa dccn) ",") dccfp)',
        '(write-line (strcat "\\"entity_type_csv\\": \\"" dcctypes "\\",") dccfp)',
        '(write-line (strcat "\\"entity_layer_csv\\": \\"" dcclayersof "\\",") dccfp)',
        '(write-line (strcat "\\"layer_csv\\": \\"" dcclayers "\\"") dccfp)',
        '(write-line "}" dccfp)',
    )
)

# Taken before the drawing commands run: `result_lines` execute after the
# result file is opened, so a "before" baseline can only be captured here.
_BASELINE_LINES = ('(setq dccbefore (if (setq dccss0 (ssget "X")) (sslength dccss0) 0))',)


#: Last-resort codec for console output that is neither valid UTF-16 nor
#: UTF-8 — on a localised Windows install the console can emit the ANSI code
#: page, so that is what an unparsable buffer most plausibly is.
_FALLBACK_ENCODING = "mbcs" if os.name == "nt" else "latin-1"


def _decode_console_bytes(raw: Optional[bytes]) -> str:
    """Decode accoreconsole output, which is UTF-16 on Windows.

    A UTF-8 decode of that stream raises UnicodeDecodeError, which would abort
    the whole operation before the result file could be read.

    The order is decided by sniffing, not by trying UTF-16 first: UTF-16
    accepts any even-length buffer, so an even-length ASCII or UTF-8 message
    decodes "successfully" into mojibake (``b"plains"`` -> ``'汰楡獮'``).
    UTF-16-encoded text interleaves NUL bytes that UTF-8 and ASCII never
    contain, so the presence of a NUL is what selects UTF-16.

    This matters more than it looks: result data travels in a UTF-8 result
    file, so the only thing this decoder ever handles is stdout/stderr — which
    is the sole diagnostic channel in the portable no-COM path. Getting it
    wrong means the failure message is unreadable exactly when it is needed.
    """
    if not raw:
        return ""
    order = ("utf-16", "utf-8") if b"\x00" in raw else ("utf-8", "utf-16")
    for encoding in order + (_FALLBACK_ENCODING,):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _lisp_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _point(values: Sequence[float]) -> str:
    return ",".join(repr(float(v)) for v in values)


def _contains_layer(layers: Sequence[str], name: str) -> bool:
    """Case-insensitive layer membership, as AutoCAD treats layer names."""
    return any(str(existing).strip().lower() == str(name).strip().lower() for existing in layers)


class CoreConsoleTransport(Transport):
    """Drives AutoCAD headlessly by scripting the core console over stdin."""

    name = "accoreconsole"
    capabilities = CORE_CONSOLE_CAPABILITIES

    def __init__(
        self,
        executable: Optional[str] = None,
        timeout_secs: float = 180.0,
        max_timeout_secs: float = 600.0,
    ) -> None:
        resolved = Path(executable).expanduser() if executable else None
        if resolved is None:
            resolved = discover().core_console_exe
        self.executable = resolved
        self.max_timeout_secs = float(max_timeout_secs)
        self.timeout_secs = min(float(timeout_secs), self.max_timeout_secs)
        self._host_version: Optional[str] = None
        self._host_version_failed: bool = False

    # -- plumbing ---------------------------------------------------------
    def is_available(self) -> bool:
        return self.executable is not None and Path(self.executable).is_file()

    def _timeout(self, requested: Optional[float]) -> float:
        timeout = self.timeout_secs if requested is None else float(requested)
        if timeout <= 0 or timeout > self.max_timeout_secs:
            raise TransportError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

    def _run_script(
        self, lines: Sequence[str], timeout_secs: Optional[float] = None
    ) -> Dict[str, Any]:
        """Pipe a CRLF script through stdin and return the run envelope."""
        if not self.is_available():
            raise TransportUnavailable(
                "accoreconsole.exe was not found; set AUTOCAD_EXE or AUTOCAD_ACCORECONSOLE_EXE"
            )
        timeout = self._timeout(timeout_secs)
        script = _LINE_ENDING.join(list(lines) + ["_.QUIT", ""])
        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            # capture_output with text=True would decode as UTF-8, but
            # accoreconsole.exe emits UTF-16; decode explicitly instead.
            completed = subprocess.run(
                [str(self.executable)],
                input=script.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(
                "AutoCAD core console exceeded the %.1f second timeout" % timeout
            ) from exc
        except OSError as exc:
            raise TransportError("AutoCAD core console could not be launched") from exc
        return {
            "returncode": int(completed.returncode),
            "duration_secs": round(time.monotonic() - started, 3),
            "stdout": _decode_console_bytes(completed.stdout)[:4096],
            "stderr": _decode_console_bytes(completed.stderr)[:4096],
        }

    def _run_with_result(
        self,
        preamble: Sequence[str],
        result_lines: Sequence[str],
        timeout_secs: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run a script that writes a JSON result file, then read it back.

        The core console has no stdout contract for our operations, so the
        script writes its structured result to a temp file via AutoLISP and the
        transport parses it. A missing or malformed file is a hard error, never
        a silent success.
        """
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-autocad-") as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            lines = list(preamble)
            lines.append('(setq dccfp (open "%s" "w"))' % _lisp_escape(str(result_path)))
            lines.extend(result_lines)
            lines.append("(close dccfp)")
            run = self._run_script(lines, timeout_secs)
            if not result_path.is_file():
                detail = (run["stderr"] or run["stdout"]).strip().splitlines()
                raise TransportError(
                    "AutoCAD core console produced no result%s"
                    % (": %s" % detail[-1] if detail else "")
                )
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransportError("AutoCAD core console returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TransportError("AutoCAD core console returned an invalid envelope")
        payload.setdefault("engine", run)
        return payload

    # -- transport surface -------------------------------------------------
    def _probe_version(self) -> Dict[str, Any]:
        """Run the version probe and return its payload."""
        return self._run_with_result(["FILEDIA", "0"], _STATUS_LINES, timeout_secs=60)

    def host_version(self) -> Optional[str]:
        """Read ACADVER out of the console, or None when it cannot be read.

        Only ever called to enrich a failure report, so an unreachable host is
        reported as unknown rather than turning into a second error.
        """
        if self._host_version is not None:
            return self._host_version
        # A failed probe is cached too. Reaching this path usually means the
        # host is already unhealthy, and _probe_version() costs up to
        # _PROBE_TIMEOUT_SECS; re-probing once per failure report (and twice
        # per WriteVerificationError) would stack minutes onto an error that
        # should surface immediately.
        if self._host_version_failed:
            return None
        try:
            payload = self._probe_version()
        except (TransportError, OSError):
            self._host_version_failed = True
            return None
        value = payload.get("version") or payload.get("acadver")
        self._host_version = str(value) if value else None
        if not self._host_version:
            self._host_version_failed = True
        return self._host_version

    def _read_back_state(self, path: Path) -> Dict[str, Any]:
        """Re-open a DWG and report what the file actually contains.

        The mutating scripts below all run inside one console session, so a
        zero exit code only proves the console quit. This is the read half of
        the write contract: it is the only check that can tell "AutoCAD saved"
        apart from "AutoCAD finished".
        """
        payload = self._run_with_result(
            ["FILEDIA", "0", "_.OPEN", str(path)],
            _READBACK_LINES,
        )
        counts: Dict[str, int] = {}
        for token in str(payload.get("entity_type_csv", "")).split(","):
            token = token.strip()
            if token:
                counts[token] = counts.get(token, 0) + 1
        layers = [
            token.strip() for token in str(payload.get("layer_csv", "")).split(",") if token.strip()
        ]
        # One entry per entity, in selection order: the layer each entity is
        # actually on. Distinct from `layers`, which is the LAYER table.
        entity_layers = [
            token.strip()
            for token in str(payload.get("entity_layer_csv", "")).split(",")
            if token.strip()
        ]
        return {
            "entity_count": int(payload.get("entity_count", 0)),
            "entity_types": counts,
            "layers": layers,
            "entity_layers": entity_layers,
        }

    def status(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "transport": self.name,
            "ready": False,
            "executable": str(self.executable) if self.executable else None,
            "capabilities": sorted(c.value for c in self.capabilities),
        }
        if not self.is_available():
            base["reason"] = "accoreconsole_not_found"
            return base
        payload = self._probe_version()
        payload.pop("engine", None)
        version = payload.get("version")
        self._host_version = str(version) if version else None
        base.update(payload)
        base["ready"] = True
        return base

    def inspect_drawing(self, path: str, max_entities: int = 1000) -> DrawingSummary:
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransportError("Drawing does not exist: %s" % source)
        state = self._read_back_state(source)
        return DrawingSummary(
            name=source.name,
            path=str(source),
            entity_count=state["entity_count"],
            entity_types=state["entity_types"],
            layers=state["layers"],
            layouts=["Model"],
        )

    def create_drawing(self, output_path: str, template: Optional[str] = None) -> DrawingSummary:
        destination = Path(output_path).expanduser()
        if destination.exists():
            raise TransportError("Output already exists: %s" % destination)
        if not destination.parent.is_dir():
            raise TransportError("Output directory does not exist: %s" % destination.parent)
        payload = self._run_with_result(
            ["FILEDIA", "0", "_.NEW", template or "acad.dwt"],
            [
                '(write-line "{" dccfp)',
                '(write-line "\\"created\\": true," dccfp)',
                '(write-line (strcat "\\"entity_count\\": " (itoa 0)) dccfp)',
                '(write-line "}" dccfp)',
                "_.SAVEAS",
                "2018",
                str(destination),
            ],
        )
        if not destination.is_file():
            engine = payload.get("engine", {})
            raise TransportError(
                "AutoCAD core console produced no drawing (returncode=%s)"
                % engine.get("returncode")
            )
        state = self._read_back_state(destination)
        # Every DWG has layer 0. Its absence after a "successful" create means
        # the file is not a usable drawing, however plausible its size looks.
        if not _contains_layer(state["layers"], "0"):
            raise WriteVerificationError(
                tool="create_drawing",
                check="layer_0_present",
                expected=["0"],
                actual=state["layers"],
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"output_path": str(destination), "template": template},
                remediation=(
                    "The console exited 0 and a file exists, but it does not contain layer 0; "
                    "the template did not resolve or the save was truncated."
                ),
            )
        return DrawingSummary(
            name=destination.name,
            path=str(destination),
            entity_count=state["entity_count"],
            entity_types=state["entity_types"],
            layers=state["layers"],
            layouts=["Model"],
        )

    def add_entities(
        self, path: str, entities: Sequence[Dict[str, Any]], layer: Optional[str] = None
    ) -> Dict[str, Any]:
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransportError("Drawing does not exist: %s" % source)
        if not entities:
            raise TransportError("entities must not be empty")

        preamble: List[str] = ["FILEDIA", "0", "_.OPEN", str(source)]
        preamble.extend(_BASELINE_LINES)
        if layer:
            preamble.extend(["_.-LAYER", "_M", str(layer), ""])
        added = 0
        for index, entity in enumerate(entities):
            kind = str(entity.get("type", "")).lower()
            if kind == "line":
                preamble.extend(
                    [
                        "_.LINE",
                        _point(entity["start"]),
                        _point(entity["end"]),
                        "",
                    ]
                )
            elif kind == "circle":
                preamble.extend(
                    ["_.CIRCLE", _point(entity["center"]), repr(float(entity["radius"]))]
                )
            elif kind == "point":
                preamble.extend(["_.POINT", _point(entity["position"])])
            elif kind == "text":
                preamble.extend(
                    [
                        "_.TEXT",
                        _point(entity["position"]),
                        repr(float(entity.get("height", 2.5))),
                        "0",
                        str(entity["text"]),
                    ]
                )
            else:
                raise TransportError("entities[%d].type is unsupported: %s" % (index, kind))
            added += 1

        preamble.extend(["_.QSAVE"])
        payload = self._run_with_result(
            preamble,
            (
                '(write-line "{" dccfp)',
                '(write-line (strcat "\\"entities_before\\": " (itoa dccbefore) ",") dccfp)',
                '(setq dccss1 (ssget "X"))',
                "(setq dccafter (if dccss1 (sslength dccss1) 0))",
                '(write-line (strcat "\\"entities_after\\": " (itoa dccafter) ",") dccfp)',
                '(write-line (strcat "\\"entities_requested\\": " (itoa %d)) dccfp)' % added,
                '(write-line "}" dccfp)',
            ),
        )
        before = int(payload.get("entities_before", 0))
        after = int(payload.get("entities_after", 0))
        state = self._read_back_state(source)

        if after - before != added:
            raise WriteVerificationError(
                tool="add_entities",
                check="entities_added_in_session",
                expected=added,
                actual=after - before,
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "requested": added, "layer": layer},
                remediation=(
                    "%d of %d requested entities did not appear in the drawing; check the "
                    "coordinates are finite and that the layer exists."
                    % (added - (after - before), added)
                ),
            )
        if state["entity_count"] != after:
            raise WriteVerificationError(
                tool="add_entities",
                check="entity_count_persisted",
                expected=after,
                actual=state["entity_count"],
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "requested": added, "layer": layer},
                remediation=(
                    "The entities were added in the session but did not survive QSAVE; check "
                    "the DWG is not read-only or held open by another session."
                ),
            )
        if layer and not _contains_layer(state["layers"], layer):
            raise WriteVerificationError(
                tool="add_entities",
                check="layer_persisted",
                expected=[str(layer)],
                actual=state["layers"],
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "layer": layer},
                remediation=(
                    "The entities were saved but the requested layer is absent; in AutoCAD a "
                    "layer must exist before an entity can be assigned to it."
                ),
            )
        # The layer existing in the table is not the same as the new entities
        # being assigned to it, so check the appended entities specifically.
        if layer:
            appended = list(state.get("entity_layers") or [])[before:after]
            if len(appended) != added:
                raise WriteVerificationError(
                    tool="add_entities",
                    check="entity_layers_readable",
                    expected=added,
                    actual=len(appended),
                    host_version=self.host_version(),
                    host_matrix=host_verdict(self.host_version()),
                    params={"path": str(source), "layer": layer, "requested": added},
                    remediation=(
                        "The per-entity layer readback returned %d entries for %d appended "
                        "entities; the drawing changed underneath the verification pass."
                        % (len(appended), added)
                    ),
                )
            wrong = [name for name in appended if name.lower() != str(layer).strip().lower()]
            if wrong:
                raise WriteVerificationError(
                    tool="add_entities",
                    check="entity_layer_persisted",
                    expected=[str(layer)] * added,
                    actual=appended,
                    host_version=self.host_version(),
                    host_matrix=host_verdict(self.host_version()),
                    params={"path": str(source), "layer": layer},
                    remediation=(
                        "The entities were saved but not all of them are on the requested "
                        "layer; in AutoCAD a layer must be current, or the entity assigned "
                        "to it, before the entity is created."
                    ),
                )

        payload.pop("engine", None)
        payload.update(
            {
                "path": str(source),
                "entities_added": added,
                "entity_types": [str(e.get("type", "")).lower() for e in entities],
                "entity_count_before": before,
                "entity_count_after": state["entity_count"],
                "verified": True,
            }
        )
        return payload

    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransportError("Drawing does not exist: %s" % source)
        requested = list(add)
        preamble: List[str] = ["FILEDIA", "0", "_.OPEN", str(source)]
        for name in requested:
            preamble.extend(["_.-LAYER", "_M", str(name), ""])
        preamble.extend(["_.QSAVE"])
        # Snapshot the table before the mutation so "created" can report which
        # layers did not already exist, instead of echoing the request.
        baseline = self._read_back_state(source)
        payload = self._run_with_result(
            preamble,
            (
                '(write-line "{" dccfp)',
                '(write-line (strcat "\\"requested\\": " (itoa %d)) dccfp)' % len(requested),
                '(write-line "}" dccfp)',
            ),
        )
        state = self._read_back_state(source)
        missing = [name for name in requested if not _contains_layer(state["layers"], name)]
        if missing:
            raise WriteVerificationError(
                tool="manage_layers",
                check="layers_persisted",
                expected=missing,
                actual=state["layers"],
                host_version=self.host_version(),
                host_matrix=host_verdict(self.host_version()),
                params={"path": str(source), "add": requested},
                remediation=(
                    "The console exited 0 but the layer names are absent after reload; the "
                    "save did not reach the file, or a layer name was rejected as invalid."
                ),
            )
        # Report what the DWG gained, read back from the file, not the request.
        existing = {str(name).strip().lower() for name in baseline["layers"]}
        created = [
            name
            for name in requested
            if str(name).strip().lower() not in existing and _contains_layer(state["layers"], name)
        ]
        payload.pop("engine", None)
        payload.update(
            {
                "path": str(source),
                "created": created,
                "layers": state["layers"],
                "verified": True,
            }
        )
        return payload

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities
