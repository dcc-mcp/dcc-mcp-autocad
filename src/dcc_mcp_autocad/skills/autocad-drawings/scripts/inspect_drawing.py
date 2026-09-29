"""Read a DWG and return bounded entity, layer, and layout metadata."""

from __future__ import annotations

from typing import Any, Dict


def main(path: str, max_entities: int = 1000) -> Dict[str, Any]:
    from dcc_mcp_autocad.skill_tools import SkillError, get_bridge, resolve_under_workspace

    if not path:
        raise SkillError("path must not be empty")
    target = resolve_under_workspace(path)
    summary = get_bridge().inspect_drawing(target, max_entities=max_entities)
    return summary.as_dict()
