"""The AutoCAD host compatibility matrix and the verdicts it produces.

The matrix is the difference between "AutoCAD 2021+" — a claim no caller can
act on — and "AutoCAD 2026 is verified, 2025 is declared but unverified, and
this build is neither". These tests hold that line: they fail if a range ever
claims support without evidence, and they pin the verdict for each class of
host version.
"""

from __future__ import annotations

import json

import pytest

from dcc_mcp_autocad.compat import (
    ALLOW_UNVERIFIED_ENV,
    SUPPORTED,
    TOO_NEW,
    TOO_OLD,
    UNKNOWN,
    UNLISTED,
    UNVERIFIED,
    allow_unverified_host,
    classify_host,
    host_verdict,
    load_matrix,
    parse_version,
    unsupported_reason,
)

SUPPORTED_ACADVER = "25.1s (LMS Tech)"


def test_parse_version_reads_acadver():
    assert parse_version(SUPPORTED_ACADVER) == (25, 1, 0)
    assert parse_version("24.3s (LMS Tech)") == (24, 3, 0)
    assert parse_version("25.0") == (25, 0, 0)
    assert parse_version("") is None
    assert parse_version("not a version") is None


@pytest.mark.parametrize(
    "acadver,expected",
    (
        (SUPPORTED_ACADVER, SUPPORTED),  # 2026, the only machine-verified build
        ("25.0s (LMS Tech)", UNVERIFIED),  # 2025
        ("24.3s (LMS Tech)", UNVERIFIED),  # 2024
        ("24.0s (LMS Tech)", UNVERIFIED),  # 2021
        ("25.2s (LMS Tech)", UNVERIFIED),  # 2027 projection
        ("26.0s (LMS Tech)", TOO_NEW),  # beyond the declared span
        ("23.3s (LMS Tech)", TOO_OLD),  # before the declared span
        ("24.5s (LMS Tech)", UNLISTED),  # inside the span, in a gap
        ("garbage", UNKNOWN),
    ),
)
def test_classify_host_verdicts(acadver, expected):
    verdict = classify_host(acadver)

    assert verdict["status"] == expected, (acadver, verdict)


def test_verified_build_names_its_year_and_evidence():
    verdict = classify_host(SUPPORTED_ACADVER)

    assert verdict["host_year"] == "2026"
    assert verdict["range"]["acadver"] == "25.1"
    assert "COM" in verdict["range"]["evidence"], "a supported range must cite evidence"
    assert verdict["breaking_changes"], "2026 carries declared host behaviour"


def test_unlisted_is_never_treated_as_supported():
    """A gap between declared ranges is outside the matrix, not inside it."""
    verdict = classify_host("24.5s (LMS Tech)")

    assert verdict["status"] == UNLISTED
    assert verdict["range"] is None


def test_unsupported_reason_names_the_override():
    """Every rejection must tell the operator how to proceed, not just refuse."""
    for acadver in ("25.0s", "26.0s", "24.5s", "garbage"):
        reason = unsupported_reason(classify_host(acadver))

        assert ALLOW_UNVERIFIED_ENV in reason, acadver


def test_unknown_status_does_not_promise_an_override():
    """UNKNOWN is not overridable, so the text must not sell the switch."""
    reason = unsupported_reason({"status": UNKNOWN, "version": "garbage", "supported_ranges": ()})

    # The variable is named to say it does not apply, not as a remedy.
    assert ALLOW_UNVERIFIED_ENV in reason
    assert "cannot be overridden" in reason
    assert not reason.rstrip().endswith("to run anyway at your own risk.")


def test_overridable_statuses_still_offer_the_switch():
    """Too-new / unlisted / unverified remain overridable and must say so."""
    for acadver in ("25.0s", "26.0s", "24.5s"):
        verdict = classify_host(acadver)
        if verdict["status"] == UNKNOWN:
            continue
        reason = unsupported_reason(verdict)
        assert "to override" in reason or ALLOW_UNVERIFIED_ENV in reason, acadver


def test_host_verdict_of_missing_version_is_none():
    assert host_verdict(None) is None
    assert host_verdict("") is None


def test_allow_unverified_host_is_off_by_default(monkeypatch):
    monkeypatch.delenv(ALLOW_UNVERIFIED_ENV, raising=False)
    assert allow_unverified_host() is False

    monkeypatch.setenv(ALLOW_UNVERIFIED_ENV, "1")
    assert allow_unverified_host() is True

    monkeypatch.setenv(ALLOW_UNVERIFIED_ENV, "no")
    assert allow_unverified_host() is False


def test_every_supported_range_carries_evidence():
    """The one rule that keeps 'supported' from becoming an opinion."""
    for entry in load_matrix()["supported_ranges"]:
        assert entry.get("evidence"), "%s claims support with no evidence" % entry.get("id")
        assert entry.get("acadver"), "%s has no ACADVER" % entry.get("id")


def test_no_range_overlaps_another():
    ranges = list(load_matrix()["supported_ranges"]) + list(load_matrix()["unverified_ranges"])
    parsed = []
    for entry in ranges:
        low = parse_version(entry["min_version"])
        high = parse_version(entry["max_version"])
        assert low is not None and high is not None, entry.get("id")
        assert low <= high, "%s has min > max" % entry.get("id")
        parsed.append((entry["id"], low, high))

    for index, (left_id, left_low, left_high) in enumerate(parsed):
        for right_id, right_low, right_high in parsed[index + 1 :]:
            assert not (left_low <= right_high and right_low <= left_high), (
                "ranges %s and %s overlap" % (left_id, right_id)
            )


def test_matrix_years_match_the_acadver_map():
    matrix = load_matrix()
    mapping = matrix["acadver_to_year"]

    for key in ("supported_ranges", "unverified_ranges"):
        for entry in matrix[key]:
            assert entry["acadver"] in mapping, "%s has no ACADVER mapping" % entry["id"]
            assert mapping[entry["acadver"]] == entry["id"], (
                "%s disagrees with acadver_to_year" % entry["id"]
            )


def test_matrix_ships_inside_the_package():
    """The matrix is data, so it has to reach the installed wheel."""
    from pathlib import Path

    import dcc_mcp_autocad.compat as compat

    matrix_path = Path(compat.MATRIX_PATH)

    assert matrix_path.is_file()
    assert matrix_path.parent == Path(compat.__file__).parent
    assert json.loads(matrix_path.read_text(encoding="utf-8"))["host"] == "autocad"


def test_unverified_ranges_explain_themselves():
    for entry in load_matrix()["unverified_ranges"]:
        assert entry.get("reason"), "%s is unverified with no stated reason" % entry.get("id")
