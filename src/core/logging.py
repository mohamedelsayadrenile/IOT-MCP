"""Logging configuration.

Records go to stderr, which is where a container runtime expects them. (The
stdout prohibition this file once enforced belonged to the stdio transport,
where stdout carried the JSON-RPC stream; over HTTP it does not.)
"""

import logging
import sys

_NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging(log_level: str) -> None:
    """Send all logging to stderr, overriding any earlier configuration.

    force=True matters: MCPServer.__init__ calls logging.basicConfig() itself,
    and because the server is built during app construction that call can land
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
