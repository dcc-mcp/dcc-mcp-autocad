"""AutoCAD host transports.

Two interchangeable implementations share one contract:
  * :mod:`com_transport` - interactive COM (full capability set)
  * :mod:`core_console` - headless ``accoreconsole.exe`` (batch only)
"""

from .base import (
    COM_CAPABILITIES,
    CORE_CONSOLE_CAPABILITIES,
    Capability,
    DrawingSummary,
    Transport,
    TransportError,
    TransportUnavailable,
)
from .com_transport import ComTransport
from .core_console import CoreConsoleTransport

__all__ = [
    "COM_CAPABILITIES",
    "CORE_CONSOLE_CAPABILITIES",
    "Capability",
    "ComTransport",
    "CoreConsoleTransport",
    "DrawingSummary",
    "Transport",
    "TransportError",
    "TransportUnavailable",
]
