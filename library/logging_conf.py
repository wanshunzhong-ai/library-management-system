"""结构化 JSON 日志 + request id。"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import Flask, g, has_request_context, request


class JsonFormatter(logging.Formatter):
    """单行 JSON，便于采集与检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if has_request_context():
            payload["request_id"] = getattr(g, "request_id", None)
            payload["method"] = request.method
            payload["path"] = request.path
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def init_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # werkzeug 的访问日志由我们自己的 request 日志替代
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def register_request_logging(app: Flask) -> None:
    logger = logging.getLogger("library.access")

    @app.before_request
    def _assign_request_id() -> None:
        import time

        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        g.started_at = time.perf_counter()

    @app.after_request
    def _log_request(response):
        import time

        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        # 静态资源太吵，不打点
        if not request.path.startswith("/static"):
            started = getattr(g, "started_at", None)
            logger.info(
                "request",
                extra={
                    "extra_fields": {
                        "status": response.status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2) if started else None,
                    }
                },
            )
        return response
