"""Logging configuration for a stdio MCP server.

stdout carries the JSON-RPC stream, so every log record must go to stderr.
"""

import logging
import sys

_NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging(log_level: str) -> None:
    """Send all logging to stderr, overriding any earlier configuration.

    force=True matters: MCPServer.__init__ calls logging.basicConfig() itself, and
    because the server object is created at module import time that call lands
    first. Without force=True this one would be a silent no-op.
    """
    logging.basicConfig(
        level=log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
