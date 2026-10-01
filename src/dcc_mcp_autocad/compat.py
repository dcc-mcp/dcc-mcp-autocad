"""Machine-readable AutoCAD host compatibility matrix.

The matrix itself lives in ``compat_matrix.json`` next to this module, so the
version verdict is data that ships inside the wheel and can be refreshed
without touching code.

Two things this module deliberately does **not** do:

* It does not invent support. A host version is only ``supported`` when a
  range declares it *and* that range carries evidence. Everything else gets an
  explicit verdict — ``unverified``, ``too_old``, ``too_new``, ``unlisted`` or
  ``unknown`` — never a shrug that resolves to "good enough".
* It does not silently degrade. The opt-in override
  (:data:`ALLOW_UNVERIFIED_ENV`) is reported in the doctor payload as
  ``host_override_active``, so an operator who enables it is visible in the
  report rather than invisible in the behaviour.

Why the matrix matters more here than for most adapters
-------------------------------------------------------

AutoCAD's ObjectARX/COM surface moves between releases (ACADVER 24.0 through
25.x so far), and the ecosystem's competitors get repeatedly broken by
claiming "2021+" as a flat range. A flat range cannot answer "is *this* build
verified?", which is the only question that decides whether an agent is
allowed to mutate a production DWG.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat_matrix.json")

#: Environment variable that lets an operator run against a host the matrix has
#: not machine-verified. Never on by default; always reported when active.
ALLOW_UNVERIFIED_ENV = "DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST"

SUPPORTED = "supported"
UNVERIFIED = "unverified"
TOO_OLD = "too_old"
TOO_NEW = "too_new"
UNLISTED = "unlisted"
UNKNOWN = "unknown"

STATUS_MESSAGES = {
    SUPPORTED: "supported",
    UNVERIFIED: "declared in the matrix but not machine-verified",
    TOO_OLD: "below the supported range",
    TOO_NEW: "above the supported range",
    UNLISTED: "inside the covered span but not in any declared range",
    UNKNOWN: "not recognised as an AutoCAD version",
}

_TRUTHY = ("1", "true", "yes", "on")

# ACADVER looks like "25.1s (LMS Tech)": a release number followed by a
# vendor/localisation suffix. Only the release number is comparable.
_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

Version = Tuple[int, int, int]


def parse_version(value: str) -> Optional[Version]:
    """Parse the leading ``major.minor[.patch]`` of an ACADVER string."""
    if not value:
        return None
    match = _RELEASE.match(str(value).strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_version(version: Version) -> str:
    return "%d.%d.%d" % version


def load_matrix(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _ranges(matrix: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    return [entry for entry in (matrix.get(key) or ()) if isinstance(entry, dict)]


def supported_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return _ranges(matrix or load_matrix(), "supported_ranges")


def unverified_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return _ranges(matrix or load_matrix(), "unverified_ranges")


def _bounds(entry: Dict[str, Any]) -> Tuple[Optional[Version], Optional[Version]]:
    return parse_version(str(entry.get("min_version", ""))), parse_version(
        str(entry.get("max_version", ""))
    )


def _range_labels(entries: List[Dict[str, Any]]) -> List[str]:
    labels: List[str] = []
    for entry in entries:
        minimum, _ = _bounds(entry)
        if minimum is None:
            continue
        label = entry.get("id") or ("%d.%d" % (minimum[0], minimum[1]))
        labels.append("AutoCAD %s" % label)
    return labels


def supported_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    return _range_labels(supported_ranges(matrix))


def unverified_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    return _range_labels(unverified_ranges(matrix))


def allow_unverified_host(environ: Optional[Dict[str, str]] = None) -> bool:
    """Return True when the operator opted into an unverified host."""
    value = (environ or os.environ).get(ALLOW_UNVERIFIED_ENV, "")
    return value.strip().lower() in _TRUTHY


def breaking_changes_for(
    version: str, matrix: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Return the declared host API breaks that apply to ``version``."""
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    if parsed is None:
        return []
    applied: List[Dict[str, Any]] = []
    for entry in matrix.get("breaking_changes") or ():
        if not isinstance(entry, dict):
            continue
        since = parse_version(str(entry.get("applies_from", "")))
        if since is None or parsed < since:
            continue
        applied.append(entry)
    return applied


def classify_host(version: str, matrix: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify a discovered AutoCAD version against the matrix.

    The result is machine readable and is embedded verbatim in the doctor and
    verify reports, so callers can gate on ``status`` instead of parsing prose.
    """
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    verdict: Dict[str, Any] = {
        "version": version,
        "parsed_version": format_version(parsed) if parsed is not None else None,
        "status": UNKNOWN,
        "matrix_version": matrix.get("matrix_version"),
        "supported_ranges": supported_range_labels(matrix),
        "unverified_ranges": unverified_range_labels(matrix),
        "range": None,
        "breaking_changes": [
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "kind": entry.get("kind"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "remediation": entry.get("remediation"),
            }
            for entry in breaking_changes_for(version, matrix)
        ],
    }
    if parsed is None:
        return verdict

    for entry in supported_ranges(matrix):
        minimum, maximum = _bounds(entry)
        if minimum is None or maximum is None:
            continue
        if minimum <= parsed <= maximum:
            verdict["status"] = SUPPORTED
            verdict["host_year"] = entry.get("id")
            verdict["range"] = {
                "id": entry.get("id"),
                "min_version": entry.get("min_version"),
                "max_version": entry.get("max_version"),
                "acadver": entry.get("acadver"),
                "evidence": entry.get("evidence"),
            }
            return verdict

    for entry in unverified_ranges(matrix):
        minimum, maximum = _bounds(entry)
        if minimum is None or maximum is None:
            continue
        if minimum <= parsed <= maximum:
            verdict["status"] = UNVERIFIED
            verdict["host_year"] = entry.get("id")
            verdict["range"] = {
                "id": entry.get("id"),
                "min_version": entry.get("min_version"),
                "max_version": entry.get("max_version"),
                "acadver": entry.get("acadver"),
                "reason": entry.get("reason"),
            }
            return verdict

    declared = supported_ranges(matrix) + unverified_ranges(matrix)
    minimums = [item for entry in declared for item in (_bounds(entry)[0],) if item is not None]
    maximums = [item for entry in declared for item in (_bounds(entry)[1],) if item is not None]
    low = min(minimums, default=None)
    high = max(maximums, default=None)
    if low is not None and parsed < low:
        verdict["status"] = TOO_OLD
    elif high is not None and parsed > high:
        verdict["status"] = TOO_NEW
    else:
        # Inside the covered span but in a gap between declared ranges. That is
        # still outside the matrix and must not be treated as supported.
        verdict["status"] = UNLISTED
    return verdict


def is_supported(version: str, matrix: Optional[Dict[str, Any]] = None) -> bool:
    return classify_host(version, matrix)["status"] == SUPPORTED


def host_verdict(version: Optional[str]) -> Optional[Dict[str, Any]]:
    """Classify ``version``, or return None when there is nothing to classify."""
    if not version:
        return None
    return classify_host(str(version))


def unsupported_reason(verdict: Dict[str, Any]) -> str:
    """Build the human- and agent-readable rejection sentence for a verdict."""
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    version = verdict.get("version") or "unknown"
    status = verdict.get("status")
    if status == UNKNOWN:
        # Deliberately does not advertise %s: an unparsable ACADVER cannot be
        # overridden (see install.py), so offering the switch would send an
        # operator after a remedy that provably does nothing.
        return (
            "AutoCAD reported an unrecognised ACADVER %r; verified range: %s. This "
            "status cannot be overridden with %s — the version string itself must "
            "be readable before any host decision can be made. Check that the "
            "resolved executable is AutoCAD (not a vertical or OEM variant "
            "returning an unexpected ACADVER), then retry."
            % (version, covered, ALLOW_UNVERIFIED_ENV)
        )
    if status == TOO_NEW:
        return (
            "AutoCAD %s is newer than the compatibility matrix (verified: %s); the "
            "ObjectARX/COM surface may have moved, so the adapter refuses to run "
            "unverified. Set %s=1 to override." % (version, covered, ALLOW_UNVERIFIED_ENV)
        )
    if status == UNVERIFIED:
        unverified = verdict.get("range") or {}
        return (
            "AutoCAD %s is declared in the compatibility matrix but has no machine "
            "evidence (verified: %s; declared-but-unverified: %s). %s Set %s=1 to "
            "override."
            % (
                version,
                covered,
                unverified.get("acadver") or "this range",
                unverified.get("reason") or "",
                ALLOW_UNVERIFIED_ENV,
            )
        )
    if status == UNLISTED:
        return (
            "AutoCAD %s is not listed in the compatibility matrix (verified: %s); "
            "the adapter refuses to run unverified. Set %s=1 to override."
            % (version, covered, ALLOW_UNVERIFIED_ENV)
        )
    return "AutoCAD %s is unsupported; verified range: %s" % (version, covered)
