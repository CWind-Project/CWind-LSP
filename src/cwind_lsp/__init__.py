"""CWind language server (LSP) built on the CWind frontend."""

from ._version import __version__
from .server import CWindLanguageServer, create_server

__all__ = ["__version__", "CWindLanguageServer", "create_server"]
