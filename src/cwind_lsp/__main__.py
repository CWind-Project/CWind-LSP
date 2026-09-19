"""Command line entry point for the CWind language server."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from ._version import __version__
from .config import Settings
from .server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cwind-lsp",
        description="Language Server Protocol implementation for CWind",
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--stdio",
        action="store_true",
        help="communicate over stdin/stdout (default)",
    )
    transport.add_argument("--tcp", metavar="HOST:PORT", help="listen on a TCP socket")
    transport.add_argument(
        "--ws", metavar="HOST:PORT", help="listen on a WebSocket socket"
    )
    parser.add_argument(
        "--std-path",
        metavar="DIR",
        help="CWind installation root owning libs/ (overrides CWIND_HOME)",
    )
    parser.add_argument("--log-file", metavar="FILE", help="write logs to FILE")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _parse_address(value: str) -> tuple[str, int]:
    if ":" not in value:
        return "localhost", int(value)
    host, _, port = value.rpartition(":")
    return host or "localhost", int(port)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: list[logging.Handler] = []
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("cwind_lsp").info("starting cwind-lsp %s", __version__)

    options = {"std_path": args.std_path} if args.std_path else None
    server = create_server(Settings.from_options(options) if options else None)
    if args.tcp:
        host, port = _parse_address(args.tcp)
        server.start_tcp(host, port)
    elif args.ws:
        host, port = _parse_address(args.ws)
        server.start_ws(host, port)
    else:
        server.start_io()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
