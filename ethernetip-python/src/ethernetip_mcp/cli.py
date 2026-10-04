"""CLI entry point for the EtherNet/IP MCP server."""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from .eip_client import ConfigError
from .server import EtherNetIPMCPServer


def main() -> None:
    # Load .env first: all configuration is read when the server is built.
    load_dotenv()
    try:
        server = EtherNetIPMCPServer()
    except ConfigError as exc:
        # stdout belongs to MCP, so the message goes to stderr.
        print(f"ethernetip-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
