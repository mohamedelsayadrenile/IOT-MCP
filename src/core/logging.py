import logging
import sys

_NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
