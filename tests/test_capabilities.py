"""Capability negotiation and explicit degradation (boundaries 5 and 6)."""

from __future__ import annotations

import pytest

from dcc_mcp_autocad.bridge import AutoCadBridge
from dcc_mcp_autocad.discovery import HostPaths
from dcc_mcp_autocad.transports import (
    COM_CAPABILITIES,
    CORE_CONSOLE_CAPABILITIES,
    Capability,
    DrawingSummary,
    Transport,
    TransportUnavailable,
)


class FakeTransport(Transport):
    """Minimal transport used to drive negotiation deterministically."""

    def __init__(self, name, capabilities, available=True):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def status(self):
        return {"transport": self.name, "ready": True}

    def inspect_drawing(self, path, max_entities=1000):
        self.calls.append(("inspect", path))
        return DrawingSummary(name="x.dwg", path=path, entity_count=0)

    def create_drawing(self, output_path, template=None):
        self.calls.append(("create", output_path))
        return DrawingSummary(name="new.dwg", path=output_path, entity_count=0)

    def add_entities(self, path, entities, layer=None):
        self.calls.append(("add", path, len(entities)))
        return {"entities_added": len(entities)}

    def manage_layers(self, path, add=()):
        self.calls.append(("layers", path, list(add)))
        return {"created": list(add)}


@pytest.fixture
def no_discovery(monkeypatch):
    monkeypatch.setattr(
        "dcc_mcp_autocad.bridge.discover",
        lambda *a, **k: HostPaths(None, None, []),
    )


def _bridge(monkeypatch, transports, force=None):
    monkeypatch.setattr(
        "dcc_mcp_autocad.bridge._build_transports",
        lambda *a, **k: transports,
    )
    bridge = AutoCadBridge(force_transport=force)
    return bridge


def test_com_capabilities_are_a_superset():
    assert CORE_CONSOLE_CAPABILITIES < COM_CAPABILITIES


def test_headless_omits_interactive_capabilities():
    missing = COM_CAPABILITIES - CORE_CONSOLE_CAPABILITIES
    assert missing == {
        Capability.LIVE_DOCUMENT,
        Capability.HWND,
        Capability.INTERACTIVE_SELECTION,
    }


def test_prefers_com_when_available(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console])

    assert bridge.transport is com
    assert bridge.degraded() == []


def test_falls_back_to_console_when_com_unavailable(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES, available=False)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console])

    assert bridge.transport is console


def test_no_com_declares_degradation_explicitly(monkeypatch, no_discovery):
    """Boundary 6: degradation is reported, never silent."""
    com = FakeTransport("com", COM_CAPABILITIES, available=False)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console])

    degraded = bridge.degraded()

    assert degraded, "degradation must be declared when COM is unavailable"
    joined = " ".join(degraded)
    assert "live document" in joined
    assert "window handle" in joined
    assert "interactive selection" in joined


def test_status_marks_degraded_mode(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES, available=False)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console])

    status = bridge.status()

    assert status["ready"] is True
    assert status["transport"] == "accoreconsole"
    assert status["degraded_mode"] is True
    assert status["degraded"]


def test_full_com_reports_no_degradation(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com])

    status = bridge.status()

    assert status["degraded_mode"] is False
    assert status["degraded"] == []
    assert len(status["capabilities"]) == len(COM_CAPABILITIES)


def test_no_transport_raises_and_reports(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES, available=False)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES, available=False)
    bridge = _bridge(monkeypatch, [com, console])

    with pytest.raises(TransportUnavailable):
        _ = bridge.transport

    status = bridge.status()

    assert status["ready"] is False
    assert status["reason"] == "no_transport_available"
    assert status["capabilities"] == []
    assert status["degraded"], "even total failure enumerates what is lost"


def test_forced_transport_is_honoured(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console], force="accoreconsole")

    assert bridge.transport is console


def test_operations_delegate_to_active_transport(monkeypatch, no_discovery):
    com = FakeTransport("com", COM_CAPABILITIES, available=False)
    console = FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES)
    bridge = _bridge(monkeypatch, [com, console])

    bridge.add_entities("/tmp/a.dwg", [{"type": "line"}])
    bridge.manage_layers("/tmp/a.dwg", ["WALLS"])

    assert ("add", "/tmp/a.dwg", 1) in console.calls
    assert ("layers", "/tmp/a.dwg", ["WALLS"]) in console.calls


def test_com_point_padding_to_3d():
    """Regression: AutoCAD rejects 2-element points in the safe array."""
    from dcc_mcp_autocad.transports.com_transport import ComTransport

    captured = {}

    class _FakeClient:
        @staticmethod
        def VARIANT(vt, value):
            captured["vt"] = vt
            captured["value"] = value
            return ("variant", vt, value)

    padding = ComTransport._point(_FakeClient, None, [1, 2])
    assert padding[2] == (1.0, 2.0, 0.0), "2D input must be padded to Z=0"

    ComTransport._point(_FakeClient, None, [1, 2, 3])
    assert captured["value"] == (1.0, 2.0, 3.0)


def test_com_point_rejects_bad_arity():
    from dcc_mcp_autocad.transports.com_transport import ComTransport

    class _FakeClient:
        @staticmethod
        def VARIANT(vt, value):
            return ("variant", vt, value)

    for bad in ([1], [1, 2, 3, 4]):
        try:
            ComTransport._point(_FakeClient, None, bad)
        except Exception as exc:  # noqa: BLE001
            assert "2 or 3 coordinates" in str(exc)
        else:
            raise AssertionError("expected arity error for %r" % (bad,))


def test_com_retry_survives_transient_busy():
    """AutoCAD returns RPC_E_CALL_REJECTED while busy; retry, do not fail."""
    from dcc_mcp_autocad.transports.com_transport import com_retry

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("busy", -2147418111)
        return "ok"

    assert com_retry(flaky, attempts=5, delay=0) == "ok"
    assert calls["n"] == 3


def test_com_retry_does_not_mask_real_errors():
    from dcc_mcp_autocad.transports.com_transport import com_retry

    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise Exception("real failure", -2145320944)

    try:
        com_retry(broken, attempts=5, delay=0)
    except Exception as exc:  # noqa: BLE001
        assert "real failure" in str(exc)
    else:
        raise AssertionError("real errors must propagate")

    assert calls["n"] == 1, "non-transient errors must not be retried"


def test_hresult_extraction():
    from dcc_mcp_autocad.transports.com_transport import _hresult

    assert _hresult(Exception("x", -2147418111)) == -2147418111
    assert _hresult(Exception("plain")) is None
