"""Append typed entities to a DWG and save it."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_REQUIRED = {
    "line": ("start", "end"),
    "circle": ("center", "radius"),
    "point": ("position",),
    "text": ("position", "text"),
}


def main(
    path: str,
    entities: List[Dict[str, Any]],
    layer: Optional[str] = None,
) -> Dict[str, Any]:
    from dcc_mcp_autocad.skill_tools import (
        SkillError,
        entity_summary,
        get_bridge,
        resolve_under_workspace,
    )

    if not path:
        raise SkillError("path must not be empty")
    if not entities:
        raise SkillError("entities must not be empty")
    if len(entities) > 500:
        raise SkillError("entities are limited to 500 per call")

    for index, entity in enumerate(entities):
        kind = str(entity.get("type", "")).lower()
        if kind not in _REQUIRED:
            raise SkillError("entities[%d].type is unsupported: %s" % (index, kind))
        for field in _REQUIRED[kind]:
            if field not in entity:
                raise SkillError("entities[%d] requires %r" % (index, field))
        if kind == "circle" and float(entity["radius"]) <= 0:
            raise SkillError("entities[%d].radius must be positive" % index)

    target = resolve_under_workspace(path)
    result = get_bridge().add_entities(target, entities, layer)
    result["requested"] = entity_summary(entities)
    return result
