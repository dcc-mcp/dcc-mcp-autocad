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
from .compat import (
    ALLOW_UNVERIFIED_ENV,
    SUPPORTED,
    TOO_NEW,
    TOO_OLD,
    UNKNOWN,
    UNLISTED,
    UNVERIFIED,
    allow_unverified_host,
    host_verdict,
    unsupported_reason,
)

MIN_CORE_VERSION = "0.20.36"
INSTALL_SOP_REPORT_SCHEMA_VERSION = 1
INSTALL_GUIDE_URL = "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-autocad/main/install.md"

EXIT_OK = 0
EXIT_NOT_READY = 10
#: Distinct from "not ready": the host answered, but its version is outside the
#: compatibility matrix. Callers must be able to tell those two apart without
#: parsing the prose in ``failure.reason``.
EXIT_HOST_UNSUPPORTED = 11
EXIT_RUNTIME_FAILURE = 40

ERROR_HOST_UNVERIFIABLE = "autocad_host_version_unavailable"
_HOST_ERROR_CODES = {
    UNVERIFIED: "autocad_host_version_unverified",
    TOO_OLD: "autocad_host_version_too_old",
    TOO_NEW: "autocad_host_version_newer_than_matrix",
    UNLISTED: "autocad_host_version_unlisted",
    UNKNOWN: "autocad_host_version_unparsable",
}

#: Stated in the report so a green CI run can never be quoted as host evidence.
_CI_BOUND = (
    "No GitHub-hosted runner has AutoCAD installed or licensed, so CI never "
    "launches the host. A green CI run is contract-level evidence only."
)
_HOST_EVIDENCE_HOW = (
    "Run `dcc-mcp-autocad-doctor verify --json` on a machine with a licensed "
    "AutoCAD, or enable the self-hosted `autocad-live` CI job."
)


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


def _host_block(status: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the host version the active transport actually reported.

    The opt-in override is honoured here and reported here. It is deliberately
    not a silent fallback: every report carries ``allow_unverified_host`` and
    ``override_active``, so an operator running against an unverified build is
    visible in the payload rather than invisible in the behaviour.
    """
    runtime = status.get("runtime") or {}
    version = runtime.get("version")
    verdict = host_verdict(version)
    override = allow_unverified_host()
    block: Dict[str, Any] = {
        "version": version,
        "status": "unavailable",
        "supported": False,
        "override_active": False,
        "allow_unverified_host": override,
        "override_env": ALLOW_UNVERIFIED_ENV,
        "matrix": verdict,
        "reason": None,
    }
    if verdict is None:
        block["reason"] = "the active transport did not report an ACADVER host version"
        return block

    block["status"] = verdict["status"]
    block["supported"] = verdict["status"] == SUPPORTED
    if not block["supported"] and override and verdict["status"] != UNKNOWN:
        block["supported"] = True
        block["override_active"] = True
    if not block["supported"]:
        block["reason"] = unsupported_reason(verdict)
    return block


def _build_report(args: argparse.Namespace, status: Dict[str, Any]) -> Dict[str, Any]:
    ready = bool(status.get("ready"))
    degraded = list(status.get("degraded") or [])
    transport = status.get("transport")
    host = _host_block(status)
    host_status = str(host["status"])
    error_code: Optional[str] = None

    if not ready:
        exit_code = EXIT_NOT_READY
        reason = str(status.get("reason") or "no_transport_available")
        stage = "runtime_verification"
    elif not host["supported"]:
        exit_code = EXIT_HOST_UNSUPPORTED
        reason = str(host["reason"])
        stage = "host_version"
        error_code = (
            ERROR_HOST_UNVERIFIABLE
            if host_status == "unavailable"
            else _HOST_ERROR_CODES.get(host_status, "autocad_host_version_unsupported")
        )
    else:
        exit_code = EXIT_OK
        reason = None
        stage = None

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
    elif exit_code == EXIT_HOST_UNSUPPORTED:
        if host_status == "unavailable":
            # No version means there is no range to opt into, so offering the
            # override here would send the operator down a dead end.
            advice = (
                "Re-run the doctor against a reachable transport; this is not an override case."
            )
        else:
            advice = (
                "Install a verified AutoCAD build, or set %s=1 to accept the risk."
                % ALLOW_UNVERIFIED_ENV
            )
        steps.append(
            _next_step(
                "review-host-matrix",
                advice,
                ["dcc-mcp-autocad-doctor", "doctor", "--json"],
                str(host["reason"]),
            )
        )

    return {
        "schema_version": INSTALL_SOP_REPORT_SCHEMA_VERSION,
        "status": "ok" if exit_code == EXIT_OK else "not_ready",
        "dcc_type": "autocad",
        "adapter_version": __version__,
        "operation": args.operation,
        "exit_code": exit_code,
        # Not `ready`: a host that answered but is outside the matrix is not
        # directly usable, and reporting otherwise is how a caller ends up
        # mutating a DWG on an unverified build.
        "directly_usable": exit_code == EXIT_OK,
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
        "host": host,
        "error_code": error_code,
        "verification": {
            "level": "contract",
            "contract_checks": (
                "import safety, transport contracts, capability logic, and post-write read-back"
            ),
            "bound": _CI_BOUND,
            "host_level_evidence": _HOST_EVIDENCE_HOW,
        },
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

    report = _build_report(args, status)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
