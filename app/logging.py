"""structlog configuration.

JSON in production so an aggregator can index ``account_id`` and ``environment_id``;
a coloured console when a developer is looking at it.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(json_output: bool, level: str = "INFO") -> None:
    """Configure structlog and the stdlib root logger once."""
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )
