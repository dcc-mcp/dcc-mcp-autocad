"""Discovery must resolve hosts by path, never via a COM ProgID."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcc_mcp_autocad import discovery


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(discovery.ACAD_EXE_ENV, raising=False)
    monkeypatch.delenv(discovery.ACCORECONSOLE_EXE_ENV, raising=False)


def test_environment_override_wins(tmp_path, monkeypatch):
    acad = tmp_path / "acad.exe"
    acad.write_bytes(b"")
    monkeypatch.setenv(discovery.ACAD_EXE_ENV, str(acad))

    result = discovery.discover()

    assert result.gui_exe == acad
    assert "environment" in result.sources


def test_console_resolved_as_sibling(tmp_path, monkeypatch):
    acad = tmp_path / "acad.exe"
    console = tmp_path / "accoreconsole.exe"
    acad.write_bytes(b"")
    console.write_bytes(b"")
    monkeypatch.setenv(discovery.ACAD_EXE_ENV, str(acad))

    result = discovery.discover()

    assert result.core_console_exe == console


def test_explicit_argument_source(tmp_path):
    acad = tmp_path / "acad.exe"
    acad.write_bytes(b"")

    result = discovery.discover(explicit_gui_exe=str(acad))

    assert result.sources == ["explicit_argument"]
    assert result.found is True


def test_console_alone_is_sufficient(tmp_path, monkeypatch):
    """A headless-only install must still be discovered as usable."""
    console = tmp_path / "accoreconsole.exe"
    console.write_bytes(b"")
    monkeypatch.setenv(discovery.ACCORECONSOLE_EXE_ENV, str(console))
    monkeypatch.setattr(
        discovery,
        "_from_known_dirs",
        lambda: discovery.HostPaths(None, console, ["known_install_dir"]),
    )

    result = discovery.discover()

    assert result.core_console_exe == console
    assert result.found is True


def test_nothing_found_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(discovery, "_from_registry", lambda: None)
    monkeypatch.setattr(discovery, "_from_known_dirs", lambda: None)

    result = discovery.discover()

    assert result.found is False
    assert result.gui_exe is None
    assert result.core_console_exe is None


def test_as_dict_is_json_safe(tmp_path):
    acad = tmp_path / "acad.exe"
    acad.write_bytes(b"")

    payload = discovery.discover(explicit_gui_exe=str(acad)).as_dict()

    assert payload["found"] is True
    assert isinstance(payload["gui_exe"], str)
    assert isinstance(payload["sources"], list)


def test_year_sorting_prefers_newest(monkeypatch, tmp_path):
    older = tmp_path / "AutoCAD 2021"
    newer = tmp_path / "AutoCAD 2026"
    for directory in (older, newer):
        directory.mkdir()
        (directory / "acad.exe").write_bytes(b"")
    monkeypatch.setattr(discovery, "_known_install_roots", lambda: [tmp_path])

    result = discovery._from_known_dirs()

    assert result is not None
    assert result.gui_exe is not None
    assert result.gui_exe.parent == newer


def test_no_com_import_on_any_platform():
    """Discovery itself must never require pywin32."""
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    assert "win32com.client" not in source
    assert "winreg" in source
    assert os.name in ("nt", "posix")
