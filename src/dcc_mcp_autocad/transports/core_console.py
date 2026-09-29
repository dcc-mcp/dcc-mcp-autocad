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

from ..discovery import discover
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


def _decode_console_bytes(raw: Optional[bytes]) -> str:
    """Decode accoreconsole output, which is UTF-16 on Windows.

    A UTF-8 decode of that stream raises UnicodeDecodeError, which would abort
    the whole operation before the result file could be read.
    """
    if not raw:
        return ""
    for encoding in ("utf-16", "utf-8", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _lisp_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _point(values: Sequence[float]) -> str:
    return ",".join(repr(float(v)) for v in values)


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
        payload = self._run_with_result(
            ["FILEDIA", "0"],
            [
                '(write-line "{" dccfp)',
                '(write-line "\\"version\\": \\"%s\\"," dccfp)' % _lisp_escape(_acadver_expr()),
                '(write-line "\\"acadver\\": \\"%s\\"" dccfp)' % _lisp_escape(_acadver_expr()),
                '(write-line "}" dccfp)',
            ],
            timeout_secs=60,
        )
        payload.pop("engine", None)
        base.update(payload)
        base["ready"] = True
        return base

    def inspect_drawing(self, path: str, max_entities: int = 1000) -> DrawingSummary:
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransportError("Drawing does not exist: %s" % source)
        payload = self._run_with_result(
            ["FILEDIA", "0", "_.OPEN", str(source)],
            [
                '(setq dccss (ssget "X"))',
                "(setq dccn (if dccss (sslength dccss) 0))",
                '(setq dcctypes "")',
                "(setq dcci 0)",
                "(while (< dcci dccn)",
                "  (setq dcent (entget (ssname dccss dcci)))",
                '  (setq dcctypes (strcat dcctypes (cdr (assoc 0 dcent)) ","))',
                "  (setq dcci (1+ dcci))",
                ")",
                '(write-line "{" dccfp)',
                '(write-line (strcat "\\"entity_count\\": " (itoa dccn) ",") dccfp)',
                '(write-line (strcat "\\"entity_type_csv\\": \\"" dcctypes "\\"") dccfp)',
                '(write-line "}" dccfp)',
            ],
        )
        engine = payload.get("engine", {})
        if engine.get("returncode", 0) != 0 and "entity_count" not in payload:
            raise TransportError("AutoCAD core console failed to inspect %s" % source)
        raw_count = int(payload.get("entity_count", 0))
        csv = str(payload.get("entity_type_csv", ""))
        counts: Dict[str, int] = {}
        for token in csv.split(","):
            token = token.strip()
            if token:
                counts[token] = counts.get(token, 0) + 1
        return DrawingSummary(
            name=source.name,
            path=str(source),
            entity_count=raw_count,
            entity_types=counts,
            layers=[],
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
        return DrawingSummary(
            name=destination.name,
            path=str(destination),
            entity_count=0,
            entity_types={},
            layers=["0"],
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
            [
                '(write-line "{" dccfp)',
                '(write-line (strcat "\\"entities_added\\": " (itoa %d)) dccfp)' % added,
                '(write-line "}" dccfp)',
            ],
        )
        payload.pop("engine", None)
        payload.update(
            {
                "path": str(source),
                "entities_added": added,
                "entity_types": [str(e.get("type", "")).lower() for e in entities],
            }
        )
        return payload

    def manage_layers(self, path: str, add: Sequence[str] = ()) -> Dict[str, Any]:
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransportError("Drawing does not exist: %s" % source)
        preamble: List[str] = ["FILEDIA", "0", "_.OPEN", str(source)]
        for name in add:
            preamble.extend(["_.-LAYER", "_M", str(name), ""])
        preamble.extend(["_.QSAVE"])
        payload = self._run_with_result(
            preamble,
            [
                '(write-line "{" dccfp)',
                '(write-line (strcat "\\"created\\": " (itoa %d)) dccfp)' % len(add),
                '(write-line "}" dccfp)',
            ],
        )
        payload.pop("engine", None)
        payload.update({"path": str(source), "created": list(add)})
        return payload

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


def _acadver_expr() -> str:
    """Static marker for the status payload; ACADVER is read via AutoLISP."""
    return "core_console"
