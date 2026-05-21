"""Project-wide logging configuration."""
from __future__ import annotations

import logging
import sys


def configure(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
