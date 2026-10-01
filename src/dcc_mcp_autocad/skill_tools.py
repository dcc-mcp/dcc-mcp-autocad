"""Shared helpers for declarative skill scripts.

Skill scripts run inside the adapter process, so host modules are imported
lazily inside callables. Importing this module must never pull in pywin32.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional


class SkillError(RuntimeError):
    """A bounded, user-actionable skill failure."""


def default_workspace() -> str:
    """Return the default writable workspace for drawing operations."""
    configured = os.environ.get("DCC_MCP_AUTOCAD_WORKSPACE")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.abspath(tempfile.gettempdir())


def resolve_under_workspace(path: str) -> str:
    """Resolve a drawing path, keeping it inside an accepted root.

    Accepted roots are the configured workspace plus any root listed in
    ``DCC_MCP_AUTOCAD_ALLOWED_ROOTS``, which may lie outside the workspace.
    The comparison is lexical (``abspath`` + ``normcase``): symlinks are not
    resolved.
    """
    candidate = os.path.abspath(os.path.expanduser(path))
    roots = [os.path.normcase(default_workspace())]
    extra = os.environ.get("DCC_MCP_AUTOCAD_ALLOWED_ROOTS", "")
    for item in extra.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(os.path.normcase(os.path.abspath(os.path.expanduser(item))))
    normalized = os.path.normcase(candidate)
    if not any(normalized == root or normalized.startswith(root + os.sep) for root in roots):
        raise SkillError("Path escapes the configured workspace; set DCC_MCP_AUTOCAD_ALLOWED_ROOTS")
    return candidate


def get_bridge(transport: Optional[str] = None):
    """Build the negotiated bridge lazily.

    Delegates to :func:`dcc_mcp_autocad.bridge.get_bridge` so the package-level
    export and the skill-facing helper can never drift apart again: they used
    to be two different functions with the same name and different first
    arguments.
    """
    from .bridge import get_bridge as _build_bridge

    return _build_bridge(transport)


def entity_summary(entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for entity in entities:
        kind = str(entity.get("type", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts
