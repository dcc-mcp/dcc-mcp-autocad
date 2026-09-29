"""Path-based AutoCAD host discovery.

Portable-first contract: locating the host must never require a registered COM
ProgID. Green/portable installs do not write ProgIDs, so discovery resolves
executables on disk first and treats COM as an optional accelerator.

Resolution order (first hit wins, but every source is reported for diagnosis):
  1. ``AUTOCAD_EXE`` environment variable (explicit operator override)
  2. Registry install location (``AcadLocation`` / ``App Paths``)
  3. Known installation directories (``C:\\Program Files\\Autodesk\\AutoCAD <year>``)

``accoreconsole.exe`` is resolved as a sibling of ``acad.exe`` because the
headless transport ships alongside the interactive binary in every AutoCAD
install. This is what makes the no-COM fallback possible on portable installs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

ACAD_EXE_ENV = "AUTOCAD_EXE"
ACCORECONSOLE_EXE_ENV = "AUTOCAD_ACCORECONSOLE_EXE"

_GUI_EXE = "acad.exe"
_CORE_CONSOLE_EXE = "accoreconsole.exe"

# Widest reasonable support window; ObjectARX 25.x covers AutoCAD 2025/2026.
_YEAR_PATTERN = re.compile(r"^AutoCAD (?P<year>(?:20[0-9]{2}))$")


@dataclass(frozen=True)
class HostPaths:
    """Resolved AutoCAD executables and where they were found."""

    gui_exe: Optional[Path]
    core_console_exe: Optional[Path]
    sources: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.gui_exe is not None or self.core_console_exe is not None

    def as_dict(self) -> dict:
        return {
            "gui_exe": str(self.gui_exe) if self.gui_exe else None,
            "core_console_exe": str(self.core_console_exe) if self.core_console_exe else None,
            "sources": list(self.sources),
            "found": self.found,
        }


def _sibling(directory: Path, name: str) -> Optional[Path]:
    candidate = directory / name
    return candidate if candidate.is_file() else None


def _from_environment() -> Optional[HostPaths]:
    """Explicit operator override; honoured before anything else."""
    explicit = os.environ.get(ACAD_EXE_ENV, "").strip()
    if not explicit:
        return None
    gui = Path(explicit).expanduser()
    if not gui.is_file():
        return None
    console_override = os.environ.get(ACCORECONSOLE_EXE_ENV, "").strip()
    console: Optional[Path] = None
    if console_override:
        candidate = Path(console_override).expanduser()
        console = candidate if candidate.is_file() else None
    if console is None:
        console = _sibling(gui.parent, _CORE_CONSOLE_EXE)
    return HostPaths(gui_exe=gui, core_console_exe=console, sources=["environment"])


def _registry_install_dirs() -> Iterable[Path]:
    """Yield candidate install directories recorded by the Autodesk installer."""
    if os.name != "nt":
        return ()
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return ()

    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    suffixes = (
        r"Software\Autodesk\AutoCAD",
        r"Software\Autodesk\AutoCAD\ACAD",
    )
    for root in roots:
        for suffix in suffixes:
            try:
                handle = winreg.OpenKey(root, suffix)
            except OSError:
                continue
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(handle, index)
                except OSError:
                    break
                index += 1
                try:
                    sub = winreg.OpenKey(handle, subkey_name)
                except OSError:
                    continue
                for value_name in ("AcadLocation", "Location", "InstallDir"):
                    try:
                        value, _ = winreg.QueryValueEx(sub, value_name)
                    except OSError:
                        continue
                    if value:
                        yield Path(str(value))
                winreg.CloseKey(sub)
            winreg.CloseKey(handle)


def _from_registry() -> Optional[HostPaths]:
    for install_dir in _registry_install_dirs():
        gui = _sibling(install_dir, _GUI_EXE)
        console = _sibling(install_dir, _CORE_CONSOLE_EXE)
        if gui or console:
            return HostPaths(
                gui_exe=gui,
                core_console_exe=console,
                sources=["registry"],
            )
    return None


def _known_install_roots() -> List[Path]:
    """Standard per-user and machine-wide Autodesk installation roots."""
    roots: List[Path] = []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    roots.append(Path(program_files) / "Autodesk")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Autodesk")
    return roots


def _year_key(name: str) -> int:
    match = _YEAR_PATTERN.match(name)
    return int(match.group("year")) if match else -1


def _from_known_dirs() -> Optional[HostPaths]:
    candidates: List[Path] = []
    for root in _known_install_roots():
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda item: _year_key(item.name), reverse=True)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith("AutoCAD "):
                continue
            gui = _sibling(entry, _GUI_EXE)
            console = _sibling(entry, _CORE_CONSOLE_EXE)
            if gui or console:
                candidates.append(entry)
    if not candidates:
        return None
    chosen = candidates[0]
    return HostPaths(
        gui_exe=_sibling(chosen, _GUI_EXE),
        core_console_exe=_sibling(chosen, _CORE_CONSOLE_EXE),
        sources=["known_install_dir"],
    )


def discover(explicit_gui_exe: Optional[str] = None) -> HostPaths:
    """Resolve AutoCAD executables without depending on any COM registration."""
    if explicit_gui_exe:
        gui = Path(explicit_gui_exe).expanduser()
        if gui.is_file():
            return HostPaths(
                gui_exe=gui,
                core_console_exe=_sibling(gui.parent, _CORE_CONSOLE_EXE),
                sources=["explicit_argument"],
            )
    sources: List[str] = []
    gui: Optional[Path] = None
    console: Optional[Path] = None

    for resolver in (_from_environment, _from_registry, _from_known_dirs):
        result = resolver()
        if result is None:
            continue
        sources.extend(result.sources)
        gui = gui or result.gui_exe
        console = console or result.core_console_exe

    return HostPaths(gui_exe=gui, core_console_exe=console, sources=sources)
