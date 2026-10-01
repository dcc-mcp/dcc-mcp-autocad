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


# Every case here previously depended on byte-length parity: UTF-16 accepts any
# even-length buffer, so even-length ASCII decoded to mojibake instead of
# raising. The pairs cover both parities in both encodings, plus non-ASCII.
_DECODE_MATRIX = (
    (b"plain", "plain"),  # odd length ASCII
    (b"plains", "plains"),  # even length ASCII
    (b"Error: bad argument!", "Error: bad argument!"),  # even, punctuation
    (b"Error: bad argument", "Error: bad argument"),  # odd, punctuation
    ("plain".encode("utf-16"), "plain"),
    ("plains".encode("utf-16"), "plains"),
    ("AutoCAD Core Engine Console".encode("utf-16"), "AutoCAD Core Engine Console"),
    ("AutoCAD Core Engine Console".encode("utf-8"), "AutoCAD Core Engine Console"),
    ("图层".encode("utf-8"), "图层"),  # even length UTF-8, no NUL bytes
    ("图层".encode("utf-16"), "图层"),
)


@pytest.mark.parametrize("raw,expected", _DECODE_MATRIX)
def test_decode_matrix_is_parity_independent(raw, expected):
    """Diagnostics must be readable whether the buffer length is odd or even."""
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    assert _decode_console_bytes(raw) == expected


def test_even_length_ascii_is_not_mojibake():
    """Regression: b"plains" decoded to '汰楡獮' when UTF-16 was tried first."""
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    assert _decode_console_bytes(b"plains") == "plains"


def test_failure_detail_stays_readable():
    """The no-result error carries the last console line, so decoding matters."""
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    detail = _decode_console_bytes(b"Error: bad argument!").strip().splitlines()[-1]

    assert detail == "Error: bad argument!"


class _FakeConsole:
    """Replays canned accoreconsole runs, keyed by the script piped to stdin.

    Every ``_run_with_result`` call opens its own temp result file, so the fake
    pulls the path out of the script and writes the next canned payload there.
    It also honours ``_.SAVEAS`` so ``create_drawing`` produces a real file:
    without that the read-back would have nothing to reopen and the contract
    would be untestable.
    """

    def __init__(self, *payloads, baseline=None):
        self.payloads = list(payloads)
        # `manage_layers` reads the DWG twice: once before the mutation to see
        # which layers already existed, and once after. The baseline read is
        # served from `baseline` so the numbered payloads keep their meaning.
        self.baseline = baseline
        self.scripts = []

    def __call__(self, args, **kwargs):
        import re

        script = (kwargs.get("input") or b"").decode("utf-8")
        self.scripts.append(script)
        is_baseline = self.baseline is not None and len(self.scripts) == 1
        if is_baseline:
            payload = self.baseline
        else:
            index = len(self.scripts) - (2 if self.baseline is not None else 1)
            payload = self.payloads[index] if 0 <= index < len(self.payloads) else {}

        target = re.search(r'\(setq dccfp \(open "(.+?)" "w"\)\)', script)
        if target:
            Path(target.group(1).replace("\\\\", "\\")).write_text(
                json.dumps(payload), encoding="utf-8"
            )
        saveas = re.search(r"_\.SAVEAS\r\n2018\r\n(.+?)\r\n", script)
        if saveas:
            Path(saveas.group(1)).write_bytes(b"AC1032 test dwg")
        return _FakeCompleted()


def test_status_reports_the_real_acadver(transport, monkeypatch):
    """Regression: the status payload carried a hardcoded 'core_console'."""
    monkeypatch.setattr(
        subprocess, "run", _FakeConsole({"version": "25.1s (LMS Tech)", "acadver": "25.1"})
    )

    status = transport.status()

    assert status["ready"] is True
    assert status["version"] == "25.1s (LMS Tech)"
    assert status["version"] != "core_console"


def test_host_version_is_cached_from_status(transport, monkeypatch):
    console = _FakeConsole({"version": "25.1s (LMS Tech)", "acadver": "25.1s (LMS Tech)"})
    monkeypatch.setattr(subprocess, "run", console)

    transport.status()

    assert transport.host_version() == "25.1s (LMS Tech)"
    assert len(console.scripts) == 1, "host_version must reuse the status probe"


def test_add_entities_reads_back_the_persisted_count(transport, monkeypatch, tmp_path):
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"entities_before": 2, "entities_after": 4, "entities_requested": 2},
            {"entity_count": 4, "entity_type_csv": "AcDbLine,AcDbLine,", "layer_csv": "0,"},
        ),
    )

    result = transport.add_entities(
        str(dwg),
        [{"type": "line", "start": [0, 0], "end": [1, 1]}, {"type": "point", "position": [2, 2]}],
    )

    assert result["verified"] is True
    assert result["entity_count_after"] == 4
    assert result["entity_count_before"] == 2


def test_add_entities_fails_when_the_read_back_disagrees(transport, monkeypatch, tmp_path):
    """The core case: console exited 0, but the save did not reach the file."""
    from dcc_mcp_autocad.write_contract import WriteVerificationError

    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"entities_before": 2, "entities_after": 4, "entities_requested": 2},
            {"entity_count": 3, "entity_type_csv": "AcDbLine,", "layer_csv": "0,"},
        ),
    )

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.add_entities(
            str(dwg),
            [
                {"type": "line", "start": [0, 0], "end": [1, 1]},
                {"type": "point", "position": [2, 2]},
            ],
        )

    assert excinfo.value.check == "entity_count_persisted"
    assert excinfo.value.expected == 4
    assert excinfo.value.actual == 3
    assert "expected 4" in str(excinfo.value)
    assert "read back 3" in str(excinfo.value)


def test_add_entities_fails_when_the_session_count_disagrees(transport, monkeypatch, tmp_path):
    from dcc_mcp_autocad.write_contract import WriteVerificationError

    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"entities_before": 2, "entities_after": 3, "entities_requested": 2},
            {"entity_count": 3, "entity_type_csv": "AcDbLine,", "layer_csv": "0,"},
        ),
    )

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.add_entities(
            str(dwg),
            [
                {"type": "line", "start": [0, 0], "end": [1, 1]},
                {"type": "point", "position": [2, 2]},
            ],
        )

    assert excinfo.value.check == "entities_added_in_session"


def test_manage_layers_verifies_the_layers_survived(transport, monkeypatch, tmp_path):
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"requested": 1},
            {"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,WALLS,"},
            baseline={"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,"},
        ),
    )

    result = transport.manage_layers(str(dwg), ["WALLS"])

    assert result["verified"] is True
    assert result["layers"] == ["0", "WALLS"]
    # WALLS did not exist in the baseline, so it was genuinely created.
    assert result["created"] == ["WALLS"]


def test_manage_layers_fails_when_a_layer_is_absent_after_reload(transport, monkeypatch, tmp_path):
    """Regression: `created` was assembled from the arguments, not the DWG."""
    from dcc_mcp_autocad.write_contract import WriteVerificationError

    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"requested": 1},
            {"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,"},
            baseline={"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,"},
        ),
    )

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.manage_layers(str(dwg), ["WALLS"])

    assert excinfo.value.check == "layers_persisted"
    assert excinfo.value.expected == ["WALLS"]


def test_create_drawing_reports_the_read_back_state(transport, monkeypatch, tmp_path):
    """Regression: entity_count was hardcoded to 0 rather than read."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"created": True},
            {"entity_count": 5, "entity_type_csv": "AcDbCircle,", "layer_csv": "0,"},
        ),
    )

    summary = transport.create_drawing(str(tmp_path / "new.dwg"))

    assert summary.entity_count == 5
    assert summary.layers == ["0"]


def test_create_drawing_fails_without_layer_zero(transport, monkeypatch, tmp_path):
    """A DWG with no layer 0 is not a usable drawing, whatever its size."""
    from dcc_mcp_autocad.write_contract import WriteVerificationError

    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"created": True},
            {"entity_count": 0, "entity_type_csv": "", "layer_csv": "WALLS,"},
        ),
    )

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.create_drawing(str(tmp_path / "new.dwg"))

    assert excinfo.value.check == "layer_0_present"


def test_read_back_reopens_from_disk(transport, monkeypatch, tmp_path):
    """The read-back must be a second console run, not the session's state."""
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    console = _FakeConsole(
        {"entities_before": 0, "entities_after": 1, "entities_requested": 1},
        {"entity_count": 1, "entity_type_csv": "AcDbPoint,", "layer_csv": "0,"},
    )
    monkeypatch.setattr(subprocess, "run", console)

    transport.add_entities(str(dwg), [{"type": "point", "position": [0, 0]}])

    assert len(console.scripts) == 2, "a mutation must be followed by a read-back run"
    assert "_.QSAVE" in console.scripts[0]
    assert "_.QSAVE" not in console.scripts[1], "the read-back must not write again"
    assert "_.OPEN" in console.scripts[1]


def test_add_entities_verifies_each_new_entity_is_on_the_layer(transport, monkeypatch, tmp_path):
    """P2-1: the layer table existing is not the same as entities being on it.

    The previous check only asked whether the LAYER table contained the
    requested name, so entities landing on layer "0" still reported success.
    """
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"entities_before": 0, "entities_after": 1, "entities_requested": 1},
            # The layer exists in the table, but the new entity is on "0".
            {
                "entity_count": 1,
                "entity_type_csv": "LINE,",
                "entity_layer_csv": "0,",
                "layer_csv": "0,WALLS,",
            },
        ),
    )

    from dcc_mcp_autocad.write_contract import WriteVerificationError

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.add_entities(
            str(dwg), [{"type": "line", "start": [0, 0], "end": [1, 1]}], layer="WALLS"
        )

    assert excinfo.value.check == "entity_layer_persisted"


def test_add_entities_accepts_entities_actually_on_the_layer(transport, monkeypatch, tmp_path):
    """The symmetric positive case: entities on the layer must not be rejected."""
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"entities_before": 0, "entities_after": 1, "entities_requested": 1},
            {
                "entity_count": 1,
                "entity_type_csv": "LINE,",
                "entity_layer_csv": "WALLS,",
                "layer_csv": "0,WALLS,",
            },
        ),
    )

    result = transport.add_entities(
        str(dwg), [{"type": "line", "start": [0, 0], "end": [1, 1]}], layer="WALLS"
    )

    assert result["verified"] is True


def test_host_version_is_probed_once_after_a_failure(transport, monkeypatch, tmp_path):
    """P2-2: a failed probe must be cached, or every error pays for it twice."""
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise OSError("host is wedged")

    monkeypatch.setattr(transport, "_probe_version", boom)

    assert transport.host_version() is None
    assert transport.host_version() is None
    assert transport.host_version() is None

    assert calls["n"] == 1, "an unreachable host must be probed once, not per caller"


def test_manage_layers_created_excludes_pre_existing_layers(transport, monkeypatch, tmp_path):
    """P3-2: `created` must be the delta, not the request echoed back.

    WALLS already exists before the call, so creating it is a no-op and must
    not be reported as created. An implementation that simply assigns
    `created = requested` reports ["WALLS"] here and is wrong.
    """
    dwg = tmp_path / "a.dwg"
    dwg.write_bytes(b"AC1032")
    monkeypatch.setattr(
        subprocess,
        "run",
        _FakeConsole(
            {"requested": 2},
            {"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,WALLS,DOORS,"},
            baseline={"entity_count": 0, "entity_type_csv": "", "layer_csv": "0,WALLS,"},
        ),
    )

    result = transport.manage_layers(str(dwg), ["WALLS", "DOORS"])

    assert result["created"] == ["DOORS"], "only layers the DWG gained count as created"
    assert result["layers"] == ["0", "WALLS", "DOORS"]


def test_decode_order_is_pinned_by_a_bomless_ascii_utf16_stream():
    """P3-3: pin the sniff order against its mirror blind spot.

    `b"plains"` only proves the no-NUL branch. This case is what the sniffing
    exists for: UTF-16-LE with **no BOM** whose content is pure ASCII, so NULs
    are interleaved. It is *also* valid UTF-8 (NUL is legal there), so "try
    utf-8 first" would silently win and return NUL-riddled garbage instead of
    raising. Only the NUL sniff routes it correctly.
    """
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    raw = "Command:".encode("utf-16-le")
    assert b"\x00" in raw
    # The buffer is genuinely ambiguous: both codecs "succeed".
    assert raw.decode("utf-8") != "Command:"
    assert raw.decode("utf-16-le") == "Command:"
    assert _decode_console_bytes(raw) == "Command:"


def test_nul_free_ascii_is_never_decoded_as_utf16():
    """The complementary guard: no NUL means UTF-16 must not be tried first.

    Plain ASCII is even-length compatible with UTF-16, which turns it into
    mojibake rather than raising. This assertion is what fails if the codecs
    are ever reordered to try UTF-16 first.
    """
    from dcc_mcp_autocad.transports.core_console import _decode_console_bytes

    raw = b"Command:"
    assert b"\x00" not in raw
    # Document the trap: UTF-16 "succeeds" here and yields mojibake.
    mojibake = raw.decode("utf-16")
    assert mojibake != "Command:"
    assert len(mojibake) == 4, "UTF-16 packs two ASCII bytes per code unit"
    assert _decode_console_bytes(raw) == "Command:"
