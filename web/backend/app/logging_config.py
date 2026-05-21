"""Structured logging with a per-run correlation ID.

Provides:
- `run_id_var`: a `ContextVar[str | None]` that the runner sets so log
  records emitted from inside a run carry `run_id` in the JSON payload.
- `JsonFormatter`: a stdlib `logging.Formatter` subclass that emits one
  JSON object per record.
- `configure_logging()`: idempotent dictConfig setup; called from the
  FastAPI lifespan.

Named `logging_config.py` (not `logging.py`) to avoid the import shadow
that would otherwise mask the stdlib `logging` module on `from .logging
import ...` inside this package.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# Set by the runner; read by the JSON formatter.
run_id_var: ContextVar[Optional[str]] = ContextVar("run_id_var", default=None)


# Standard LogRecord attributes we never want to duplicate in the "extras".
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Attach run_id from ContextVar (set by the runner) if present.
        rid = run_id_var.get()
        if rid is not None:
            payload["run_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Forward any caller-supplied extras.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, default=str)


_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Idempotent dictConfig setup. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": JsonFormatter},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "json",
                },
            },
            "loggers": {
                # Quiet known-chatty libraries; runner emits its own structured logs.
                "uvicorn.access": {"level": "WARNING", "propagate": True},
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
            },
            "root": {"level": level, "handlers": ["stdout"]},
        }
    )
    _CONFIGURED = True


__all__ = ["run_id_var", "JsonFormatter", "configure_logging"]
