"""Read-only AutoCAD doctor.

Reports discovery, the negotiated transport, and the exact capability set. When
only the headless transport is usable the report declares the degraded
capabilities instead of reporting a generic success.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from .__version__ import __version__
from .bridge import AutoCadBridge

MIN_CORE_VERSION = "0.20.36"
INSTALL_SOP_REPORT_SCHEMA_VERSION = 1
INSTALL_GUIDE_URL = "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-autocad/main/install.md"

EXIT_OK = 0
EXIT_NOT_READY = 10
EXIT_RUNTIME_FAILURE = 40


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the AutoCAD runtime; no host plug-in is installed."
    )
    parser.add_argument("operation", nargs="?", choices=("doctor", "verify"), default="doctor")
    parser.add_argument("--json", action="store_true", help="Emit the stable JSON contract.")
    parser.add_argument("--exe", help="Exact acad.exe to verify.")
    parser.add_argument(
        "--transport",
        choices=("com", "accoreconsole"),
        help="Force a specific transport instead of negotiating.",
    )
    return parser


def _next_step(identifier: str, description: str, command: List[str], why: str) -> Dict[str, Any]:
    return {
        "id": identifier,
        "description": description,
        "command": command,
        "why": why,
        "action": identifier,
        "executes_install": False,
    }


def _build_report(
    args: argparse.Namespace, bridge: AutoCadBridge, status: Dict[str, Any]
) -> Dict[str, Any]:
    ready = bool(status.get("ready"))
    degraded = list(status.get("degraded") or [])
    transport = status.get("transport")

    if ready and degraded:
        exit_code = EXIT_OK
        reason = None
        stage = None
    elif ready:
        exit_code = EXIT_OK
        reason = None
        stage = None
    else:
        exit_code = EXIT_NOT_READY
        reason = str(status.get("reason") or "no_transport_available")
        stage = "runtime_verification"

    steps: List[Dict[str, Any]] = []
    if not ready:
        steps.append(
            _next_step(
                "locate-autocad",
                "Set AUTOCAD_EXE to the AutoCAD acad.exe, then rerun the doctor.",
                ["dcc-mcp-autocad-doctor", "doctor", "--json"],
                "No usable transport was found for this host.",
            )
        )

    return {
        "schema_version": INSTALL_SOP_REPORT_SCHEMA_VERSION,
        "status": "ok" if ready else "not_ready",
        "dcc_type": "autocad",
        "adapter_version": __version__,
        "operation": args.operation,
        "exit_code": exit_code,
        "directly_usable": ready,
        "adapter": {
            "name": "dcc-mcp-autocad",
            "version": __version__,
            "runtime_shape": "standalone",
            "contract": "core_install_sop_v1_verify_only",
            "host_pattern": "external_bridge_host",
        },
        "requirements": {"min_core_version": MIN_CORE_VERSION},
        "compatibility": {"mode": "verify_only", "writes_performed": False},
        "discovery": status.get("discovery", {}),
        "transport": {
            "active": transport,
            "checked": status.get("checked_transports", []),
            "failures": status.get("transport_failures", {}),
        },
        "capabilities": status.get("capabilities", []),
        "degraded": degraded,
        "degraded_mode": bool(status.get("degraded_mode")),
        "failure": {"stage": stage, "reason": reason} if reason else None,
        "blocker": ({"reason": reason, "instructions_url": INSTALL_GUIDE_URL} if reason else None),
        "next_steps": steps,
        "runtime": status.get("runtime", status),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    bridge: Optional[AutoCadBridge] = None
    status: Dict[str, Any] = {
        "ready": False,
        "reason": "doctor_exception",
        "discovery": {},
        "checked_transports": [],
        "capabilities": [],
        "degraded": [],
    }
    try:
        # Construction is inside the guard: discovery touches the registry and
        # filesystem, and a failure there must still produce a valid report.
        bridge = AutoCadBridge(force_transport=args.transport)
        status = bridge.status()
    except Exception as exc:  # noqa: BLE001 - doctor must never traceback
        status = {
            "ready": False,
            "reason": "doctor_exception",
            "detail": str(exc),
            "discovery": bridge.paths.as_dict() if bridge is not None else {},
            "checked_transports": (
                [t.name for t in bridge._candidates] if bridge is not None else []
            ),
            "capabilities": [],
            "degraded": [],
        }

    if args.exe:
        status["discovery"] = dict(status.get("discovery") or {})
        status["discovery"]["requested_exe"] = args.exe

    report = _build_report(args, bridge, status)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
