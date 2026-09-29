"""Report AutoCAD discovery, negotiated transport, and active capabilities."""

from __future__ import annotations

from typing import Any, Dict, Optional


def main(transport: Optional[str] = None) -> Dict[str, Any]:
    from dcc_mcp_autocad.skill_tools import get_bridge

    bridge = get_bridge(transport)
    status = bridge.status()
    return {
        "ready": status.get("ready", False),
        "transport": status.get("transport"),
        "capabilities": status.get("capabilities", []),
        "degraded": status.get("degraded", []),
        "degraded_mode": status.get("degraded_mode", False),
        "discovery": status.get("discovery", {}),
        "reason": status.get("reason"),
        "runtime": status.get("runtime", {}),
    }
