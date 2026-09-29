"""Package-level guarantees: import safety and version consistency."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _read_toml_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.MULTILINE)
    assert match, "pyproject.toml must declare a version"
    return match.group(1)


def test_version_matches_pyproject():
    from dcc_mcp_autocad.__version__ import __version__

    assert __version__ == _read_toml_version()


def test_public_exports():
    import dcc_mcp_autocad

    for name in ("AutoCadBridge", "AutoCadMcpServer", "get_bridge", "__version__"):
        assert hasattr(dcc_mcp_autocad, name)


def test_import_does_not_require_pywin32():
    """Importing the package must not pull in Windows-only COM modules."""
    for name in ("pythoncom", "win32com.client"):
        assert name not in sys.modules, "%s imported at package import time" % name


def test_com_transport_module_imports_without_pywin32():
    """The COM transport must import lazily to stay safe on Linux."""
    import importlib

    module = importlib.import_module("dcc_mcp_autocad.transports.com_transport")
    assert hasattr(module, "ComTransport")
    assert hasattr(module, "PROG_ID_CANDIDATES")


def test_prog_id_chain_is_version_tolerant():
    """Boundary 2: ProgID resolution must have more than one candidate."""
    from dcc_mcp_autocad.transports.com_transport import PROG_ID_CANDIDATES

    assert len(PROG_ID_CANDIDATES) >= 2
    assert PROG_ID_CANDIDATES[-1] == "AutoCAD.Application", (
        "the unversioned ProgID must terminate the fallback chain"
    )


def test_skill_package_files_exist():
    skills = ROOT / "src" / "dcc_mcp_autocad" / "skills" / "autocad-drawings"
    assert (skills / "SKILL.md").is_file()
    assert (skills / "tools.yaml").is_file()
    scripts = skills / "scripts"
    for name in (
        "get_status.py",
        "inspect_drawing.py",
        "create_drawing.py",
        "manage_layers.py",
        "add_entities.py",
    ):
        assert (scripts / name).is_file(), name


def test_no_vertical_specific_api_references():
    """Boundary 1: vanilla COM surface only, no vertical/OEM APIs."""
    src = ROOT / "src" / "dcc_mcp_autocad"
    banned = ("AecArch", "AecBase", "ACAOE", "AcArchitecture", "AecX")
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, "%s references vertical API %s" % (path.name, token)
