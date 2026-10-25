"""JSON structured logging with secret redaction."""

from __future__ import annotations
import logging
import sys
import re
import structlog

_REDACT = [
    (re.compile(r"(?i)(private[_-]?key['\"\s:=]+)([0-9a-fx]+)"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?secret['\"\s:=]+)([^'\"\s,}]+)"), r"\1***"),
    (re.compile(r"(?i)(token['\"\s:=]+)([A-Za-z0-9:_-]{8,})"), r"\1***"),
]


def _redact(_, __, event_dict):
    for k in list(event_dict.keys()):
        lk = k.lower()
        if lk in (
            "private_key",
            "api_secret",
            "token",
            "telegram_bot_token",
            "aster_api_secret",
            "hyperliquid_private_key",
        ):
            event_dict[k] = "***"
    msg = str(event_dict.get("event", ""))
    for pat, rep in _REDACT:
        msg = pat.sub(rep, msg)
    event_dict["event"] = msg
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            _redact,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str = "gg_bot"):
    return structlog.get_logger(name)
