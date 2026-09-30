"""Doctor contract: exit codes, JSON shape, and declared degradation."""

from __future__ import annotations

import json

import pytest

from dcc_mcp_autocad import install
from dcc_mcp_autocad.discovery import HostPaths
from dcc_mcp_autocad.transports import (
    COM_CAPABILITIES,
    CORE_CONSOLE_CAPABILITIES,
    Capability,
    DrawingSummary,
    Transport,
)


class FakeTransport(Transport):
    def __init__(self, name, capabilities, available=True, version="25.1s"):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.available = available
        self.version = version

    def is_available(self):
        return self.available

    def status(self):
        return {"transport": self.name, "ready": True, "version": self.version}

    def inspect_drawing(self, path, max_entities=1000):
        return DrawingSummary(name="x.dwg", path=path, entity_count=0)

    def create_drawing(self, output_path, template=None):
        return DrawingSummary(name="n.dwg", path=output_path, entity_count=0)

    def add_entities(self, path, entities, layer=None):
        return {"entities_added": len(entities)}

    def manage_layers(self, path, add=()):
        return {"created": list(add)}


@pytest.fixture
def _no_discovery(monkeypatch):
    monkeypatch.setattr(
        "dcc_mcp_autocad.bridge.discover",
        lambda *a, **k: HostPaths(None, None, []),
    )


def _install(monkeypatch, transports, argv):
    monkeypatch.setattr(
        "dcc_mcp_autocad.bridge._build_transports",
        lambda *a, **k: transports,
    )
    return install.main(argv)


def test_ready_com_exits_zero(monkeypatch, _no_discovery, capsys):
    transports = [FakeTransport("com", COM_CAPABILITIES)]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["exit_code"] == 0
    assert report["directly_usable"] is True
    assert report["degraded"] == []
    assert report["degraded_mode"] is False


def test_headless_exits_zero_but_declares_degradation(monkeypatch, _no_discovery, capsys):
    """Portable/no-COM hosts are usable, but must say what they lost."""
    transports = [
        FakeTransport("com", COM_CAPABILITIES, available=False),
        FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES),
    ]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["directly_usable"] is True
    assert report["transport"]["active"] == "accoreconsole"
    assert report["degraded_mode"] is True
    assert report["degraded"], "degradation must never be empty in headless mode"
    assert Capability.HWND.value not in report["capabilities"]


def test_no_transport_exits_ten(monkeypatch, _no_discovery, capsys):
    transports = [
        FakeTransport("com", COM_CAPABILITIES, available=False),
        FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES, available=False),
    ]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 10
    assert report["exit_code"] == 10
    assert report["directly_usable"] is False
    assert report["failure"]["reason"] == "no_transport_available"
    assert report["next_steps"], "failures must offer a concrete next step"


def test_report_declares_verify_only(monkeypatch, _no_discovery, capsys):
    transports = [FakeTransport("com", COM_CAPABILITIES)]
    _install(monkeypatch, transports, ["verify", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert report["compatibility"]["mode"] == "verify_only"
    assert report["compatibility"]["writes_performed"] is False
    assert report["adapter"]["host_pattern"] == "external_bridge_host"
    assert report["dcc_type"] == "autocad"


def test_forced_transport_flag(monkeypatch, _no_discovery, capsys):
    transports = [
        FakeTransport("com", COM_CAPABILITIES),
        FakeTransport("accoreconsole", CORE_CONSOLE_CAPABILITIES),
    ]
    _install(monkeypatch, transports, ["doctor", "--json", "--transport", "accoreconsole"])

    report = json.loads(capsys.readouterr().out)

    assert report["transport"]["active"] == "accoreconsole"
    assert report["degraded_mode"] is True


def test_exceptions_never_traceback(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("host exploded")

    monkeypatch.setattr("dcc_mcp_autocad.bridge.discover", boom)

    code = install.main(["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 10
    assert report["failure"]["reason"] == "doctor_exception"
    assert report["directly_usable"] is False


def test_verified_host_reports_its_matrix_status(monkeypatch, _no_discovery, capsys):
    transports = [FakeTransport("com", COM_CAPABILITIES, version="25.1s (LMS Tech)")]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["host"]["version"] == "25.1s (LMS Tech)"
    assert report["host"]["status"] == "supported"
    assert report["host"]["supported"] is True
    assert report["host"]["matrix"]["host_year"] == "2026"
    assert report["error_code"] is None


def test_unverified_host_version_exits_eleven(monkeypatch, _no_discovery, capsys):
    """A declared-but-unverified build is refused, not shrugged at."""
    transports = [FakeTransport("com", COM_CAPABILITIES, version="25.0s (LMS Tech)")]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 11
    assert report["exit_code"] == 11
    assert report["directly_usable"] is False
    assert report["host"]["status"] == "unverified"
    assert report["error_code"] == "autocad_host_version_unverified"
    assert report["failure"]["stage"] == "host_version"
    assert "DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST" in report["failure"]["reason"]
    assert report["next_steps"], "refusals must offer a concrete next step"


def test_override_accepts_an_unverified_host_and_says_so(monkeypatch, _no_discovery, capsys):
    monkeypatch.setenv("DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST", "1")
    transports = [FakeTransport("com", COM_CAPABILITIES, version="25.0s (LMS Tech)")]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["host"]["override_active"] is True
    assert report["host"]["allow_unverified_host"] is True
    # The override is visible; it is not a silent downgrade.
    assert report["host"]["status"] == "unverified"


def test_unparsable_version_is_never_overridden(monkeypatch, _no_discovery, capsys):
    """An ACADVER nobody can parse has no range to opt into."""
    monkeypatch.setenv("DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST", "1")
    transports = [FakeTransport("com", COM_CAPABILITIES, version="garbage")]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 11
    assert report["host"]["status"] == "unknown"
    assert report["host"]["override_active"] is False
    assert report["error_code"] == "autocad_host_version_unparsable"


def test_missing_host_version_is_refused(monkeypatch, _no_discovery, capsys):
    transports = [FakeTransport("com", COM_CAPABILITIES, version=None)]
    code = _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)

    assert code == 11
    assert report["host"]["status"] == "unavailable"
    assert report["error_code"] == "autocad_host_version_unavailable"


def test_report_states_the_ci_bound(monkeypatch, _no_discovery, capsys):
    """A green CI run must never be quotable as host-level evidence."""
    transports = [FakeTransport("com", COM_CAPABILITIES)]
    _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)
    verification = report["verification"]

    assert verification["level"] == "contract"
    assert "github-hosted runner" in verification["bound"].lower()
    assert "AutoCAD" in verification["bound"]
    assert "autocad-live" in verification["host_level_evidence"]


def test_missing_version_next_step_does_not_offer_the_override(monkeypatch, _no_discovery, capsys):
    """With no version there is no range to opt into; do not imply there is."""
    transports = [FakeTransport("com", COM_CAPABILITIES, version=None)]
    _install(monkeypatch, transports, ["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)
    why = report["next_steps"][0]["description"]

    assert "DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST" not in why
    assert "not an override case" in why
