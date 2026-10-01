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
NON_TOOL_METHODS = {
    "is_available",
    "supports",
    "host_version",
    # ComTransport only: drops the COM session handle. It writes nothing to a
    # drawing and runs after the drawing has already been verified, so it owes
    # no read-back. Named here rather than left for the next reader to find.
    "close",
}


def _transport_modules():
    """Import every module in the transports package and return them.

    The list is read from the package rather than written out here. A
    hand-maintained import list rots silently: ``__subclasses__()`` only sees
    classes whose module has been *imported*, so dropping one entry still
    passes whenever some other test happens to import that module first --
    which is exactly the gap this guard exists to close.
    """
    import importlib
    import pkgutil

    import dcc_mcp_autocad.transports as transports

    return [
        importlib.import_module("dcc_mcp_autocad.transports." + info.name)
        for info in pkgutil.iter_modules(transports.__path__)
    ]


def _transport_classes():
    """Every Transport subclass defined in the package, base included.

    Classes are collected per module rather than via ``__subclasses__()`` so
    the result is the same whether or not the package re-exports them.
    """
    from dcc_mcp_autocad.transports import Transport

    ordered = [Transport]
    for module in _transport_modules():
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, Transport) and value not in ordered:
                ordered.append(value)
    return ordered


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
    would notice that it can report success without proving anything. The walk
    covers subclasses as well as the base: ``vars(Transport)`` alone never sees
    a method defined on a subclass, which is exactly where a new transport-
    specific write tool would appear.
    """
    classified = set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS)
    unclassified = {
        cls.__name__: sorted(own - classified - NON_TOOL_METHODS)
        for cls in _transport_classes()
        for own in (
            {
                name
                for name, value in vars(cls).items()
                if not name.startswith("_") and callable(value)
            },
        )
        if own - classified - NON_TOOL_METHODS
    }

    assert unclassified == {}, (
        "unclassified transport method: it has no answer to 'does this owe a "
        "post-write read-back?' -> %r" % (unclassified,)
    )
    assert set(MUTATING_TOOLS) & set(READ_ONLY_TOOLS) == set()


def test_the_classifier_guard_sees_subclass_methods():
    """Non-vacuity: the guard must cover every transport the package defines.

    Two directions have to hold. The walk must reach the known subclasses, or
    the classification test guards only the base class while looking complete;
    and every module the package ships must actually be walked, so a module
    dropped from the scan is caught here instead of rotting unnoticed.
    """
    names = {cls.__name__ for cls in _transport_classes()}

    assert {"Transport", "ComTransport", "CoreConsoleTransport"} <= names, names

    scanned = {module.__name__.rsplit(".", 1)[-1] for module in _transport_modules()}
    walked = {cls.__module__.rsplit(".", 1)[-1] for cls in _transport_classes()}
    assert {"base", "com_transport", "core_console"} <= scanned, scanned
    # Every module defining a transport must be represented in the walk.
    assert scanned & walked == {"base", "com_transport", "core_console"}, (
        scanned,
        walked,
    )


def test_mutation_classification_covers_the_documented_tools():
    assert set(MUTATING_TOOLS) == {"create_drawing", "add_entities", "manage_layers"}
    assert "inspect_drawing" in READ_ONLY_TOOLS
    assert "status" in READ_ONLY_TOOLS


def test_numbers_match_is_not_a_rubber_stamp():
    """Sanity: the default tolerance is far tighter than any drafting delta."""
    assert not numbers_match(0.0, 1e-3)
    assert math.isclose(0.0, 1e-12, rel_tol=1e-6, abs_tol=1e-9)
