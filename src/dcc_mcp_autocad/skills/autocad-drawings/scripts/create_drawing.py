"""Create a new DWG from a template and return its summary."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def main(output_path: str, template: Optional[str] = None) -> Dict[str, Any]:
    from dcc_mcp_autocad.skill_tools import SkillError, get_bridge, resolve_under_workspace

    if not output_path:
        raise SkillError("output_path must not be empty")
    if os.path.splitext(output_path)[1].lower() != ".dwg":
        raise SkillError("output_path must end with .dwg")
    target = resolve_under_workspace(output_path)
    summary = get_bridge().create_drawing(target, template)
    return summary.as_dict()
