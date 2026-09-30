"""The post-write read-back contract itself, independent of any transport.

The transport-specific halves live in ``test_core_console.py`` (accoreconsole)
and ``test_com_write_back.py`` (COM). This file covers the shared machinery and
the classification that forces a new transport method to declare whether it
owes a read-back.
"""

from __future__ import annotations

import math

from dcc_mcp_autocad.write_contract import (
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    WriteVerificationError,
    format_message,
    jsonable,
    numbers_match,
    sequences_match,
)

# Transport plumbing, not tools: they observe or describe the transport and
# change no drawing, so they owe no read-back and are excluded from the
# classification test deliberately rather than by omission.
NON_TOOL_METHODS = {"is_available", "supports", "host_version"}


def test_numbers_match_tolerance():
    assert numbers_match(1.0, 1.0)
    assert numbers_match(1.0, 1.0 + 1e-12)
    assert not numbers_match(1.0, 1.1)
    assert not numbers_match(1.0, "not a number")
    assert not numbers_match(None, None)


def test_sequences_match_rejects_length_mismatch():
    """A truncated comparison would hide the very difference it reports."""
    assert sequences_match([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert not sequences_match([1.0, 2.0, 3.0], [1.0, 2.0])
    assert not sequences_match([1.0, 2.0], [1.0, 2.0, 3.0])
    assert not sequences_match([1.0, 2.0], "nope")


def test_jsonable_keeps_non_finite_values_visible():
    """NaN is not valid JSON; dropping it would empty out the evidence."""
    assert jsonable(float("nan")) == repr(float("nan"))
    assert jsonable(float("inf")) == repr(float("inf"))
    assert jsonable({"a": (1, 2)}) == {"a": [1, 2]}
    assert jsonable(3) == 3


def test_format_message_states_expected_actual_and_host():
    message = format_message(
        {
            "tool": "add_entities",
            "check": "entity_count_persisted",
            "expected": 7,
            "actual": 6,
            "host_version": "25.1s (LMS Tech)",
            "host_matrix": {"status": "supported"},
            "remediation": "Check the DWG is not read-only.",
        }
    )

    assert "add_entities" in message
    assert "entity_count_persisted" in message
    assert "expected 7" in message
    assert "read back 6" in message
    assert "25.1s (LMS Tech)" in message
    assert "supported" in message
    assert "read-only" in message


def test_error_payload_is_structured_and_round_trips():
    error = WriteVerificationError(
        tool="manage_layers",
        check="layers_persisted",
        expected=["WALLS"],
        actual=["0"],
        host_version="25.1s (LMS Tech)",
        host_matrix={"status": "supported"},
        params={"path": "a.dwg"},
    )

    assert error.tool == "manage_layers"
    assert error.check == "layers_persisted"
    assert error.expected == ["WALLS"]
    assert error.actual == ["0"]
    assert error.host_version == "25.1s (LMS Tech)"
    assert error.payload["schema_version"] == 1

    restored = WriteVerificationError.from_payload(dict(error.payload))
    assert restored.expected == ["WALLS"]
    assert str(restored) == str(error)


def test_every_transport_method_is_classified():
    """A new transport method must declare whether it owes a read-back.

    Without this, adding a mutating method is silent: nothing in the suite
    would notice that it can report success without proving anything.
    """
    from dcc_mcp_autocad.transports import Transport

    own = {
        name
        for name, value in vars(Transport).items()
        if not name.startswith("_") and callable(value)
    }
    classified = set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS)

    assert own - classified - NON_TOOL_METHODS == set(), (
        "unclassified Transport method: it has no answer to 'does this owe a post-write read-back?'"
    )
    assert set(MUTATING_TOOLS) & set(READ_ONLY_TOOLS) == set()


def test_mutation_classification_covers_the_documented_tools():
    assert set(MUTATING_TOOLS) == {"create_drawing", "add_entities", "manage_layers"}
    assert "inspect_drawing" in READ_ONLY_TOOLS
    assert "status" in READ_ONLY_TOOLS


def test_numbers_match_is_not_a_rubber_stamp():
    """Sanity: the default tolerance is far tighter than any drafting delta."""
    assert not numbers_match(0.0, 1e-3)
    assert math.isclose(0.0, 1e-12, rel_tol=1e-6, abs_tol=1e-9)
