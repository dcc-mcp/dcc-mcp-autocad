"""Portable-first AutoCAD adapter for the DCC Model Context Protocol."""

from .__version__ import __version__
from .bridge import AutoCadBridge, get_bridge
from .server import AutoCadMcpServer

__all__ = ["AutoCadBridge", "AutoCadMcpServer", "get_bridge", "__version__"]
