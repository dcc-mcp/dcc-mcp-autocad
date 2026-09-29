"""Ensure layers exist on a DWG and return the resulting layer list."""

from __future__ import annotations

from typing import Any, Dict, List


def main(path: str, add: List[str] | None = None) -> Dict[str, Any]:
    from dcc_mcp_autocad.skill_tools import SkillError, get_bridge, resolve_under_workspace

    if not path:
        raise SkillError("path must not be empty")
    names = list(add or [])
    for name in names:
        if not str(name).strip():
            raise SkillError("layer names must not be empty")
        if len(str(name)) > 255:
            raise SkillError("layer names are limited to 255 characters")
    target = resolve_under_workspace(path)
    return get_bridge().manage_layers(target, names)
