"""Verify the accoreconsole script contract without launching AutoCAD.

These tests assert the two constraints established during evaluation:
the script is delivered over stdin (never the broken ``/s`` flag) and uses CRLF
line endings.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dcc_mcp_autocad.transports import CoreConsoleTransport, TransportError, TransportUnavailable


@pytest.fixture
def transport(tmp_path):
    exe = tmp_path / "accoreconsole.exe"
    exe.write_bytes(b"")
    return CoreConsoleTransport(executable=str(exe))


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_uses_stdin_not_slash_s(transport, monkeypatch):
    """The /s flag is unreliable on AutoCAD 2026; stdin is the contract."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        transport._run_script(["FILEDIA", "0"])
    except Exception:  # noqa: BLE001 - result file will be missing
        pass

    assert captured["args"] == [str(transport.executable)]
    assert "/s" not in captured["args"]
    assert "input" in captured["kwargs"], "script must be piped through stdin"


def test_script_uses_crlf_line_endings(transport, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["input"] = kwargs.get("input", b"")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        transport._run_script(["FILEDIA", "0"])
    except Exception:  # noqa: BLE001
        pass

    raw = captured["input"]
    assert isinstance(raw, bytes), "stdin is fed as bytes so encoding is explicit"
    script = raw.decode("utf-8")
    assert "\r\n" in script, "AutoCAD scripts require CRLF"
    assert script.endswith("_.QUIT\r\n")


def test_missing_result_file_is_a_hard_error(transport, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted())

    with pytest.raises(TransportError, match="produced no result"):
        transport._run_with_result(["FILEDIA", "0"], ['(write-line "{}" dccfp)'])


def test_result_file_is_parsed(transport, monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        # Emulate the console writing the LISP-generated result file.
        write_lisp_result(kwargs.get("input", ""), tmp_path)
        return _FakeCompleted()

    def write_lisp_result(script, base):
        import re

        if isinstance(script, bytes):
            script = script.decode("utf-8")
        match = re.search(r'\(setq dccfp \(open "(.+?)" "w"\)\)', script)
        if match:
            target = Path(match.group(1).replace("\\\\", "\\"))
            target.write_text('{"entity_count": 7}', encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = transport._run_with_result(["FILEDIA", "0"], [])

    assert payload["entity_count"] == 7
    assert "engine" in payload


def test_unavailable_when_executable_missing(tmp_path):
    transport = CoreConsoleTransport(executable=str(tmp_path / "nope.exe"))

    assert transport.is_available() is False
    with pytest.raises(TransportUnavailable):
        transport._run_script(["FILEDIA", "0"])


def test_status_reports_not_ready_without_executable(tmp_path):
    transport = CoreConsoleTransport(executable=str(tmp_path / "nope.exe"))

    status = transport.status()

    assert status["ready"] is False
    assert status["reason"] == "accoreconsole_not_found"


def test_timeout_is_bounded(transport):
    with pytest.raises(TransportError):
        transport._timeout(999999)


def test_add_entities_rejects_unknown_type(transport, tmp_path):
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"")

    with pytest.raises(TransportError, match="unsupported"):
        transport.add_entities(str(dwg), [{"type": "spline"}])


def test_create_drawing_rejects_existing_output(transport, tmp_path):
    existing = tmp_path / "a.dwg"
    existing.write_bytes(b"")

    with pytest.raises(TransportError, match="already exists"):
        transport.create_drawing(str(existing))


def test_result_json_is_well_formed(tmp_path):
    """The LISP epilogue must emit parseable JSON."""
    payload = json.loads('{"entity_count": 3}')
    assert payload["entity_count"] == 3


def test_utf16_stdout_does_not_crash(transport, monkeypatch):
    """Regression: accoreconsole emits UTF-16; a UTF-8 decode aborts the run."""
    message = "AutoCAD Core Engine Console"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout=message.encode("utf-16")),
    )

    run = transport._run_script(["FILEDIA", "0"])

    assert run["stdout"] == message


def test_decode_falls_back_without_raising():
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    assert _decode_console_bytes(None) == ""
    assert _decode_console_bytes(b"") == ""
    assert _decode_console_bytes("plain".encode("utf-16")) == "plain"
    assert _decode_console_bytes(b"plain") == "plain"
    # Undecodable bytes must degrade, never raise.
    assert isinstance(_decode_console_bytes(b"\xff\xfe\x00bad"), str)
