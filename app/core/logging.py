import json
import logging
import time
from contextvars import ContextVar, Token

from opentelemetry import trace


_log_context: ContextVar[dict[str, str]] = ContextVar("agent_log_context", default={})
_ID_FIELDS = ("tenant_id", "customer_ref", "ticket_id", "request_id", "task_id")


def bind_log_context(**ids: object) -> Token:
    """Attach trusted correlation IDs to logs in this async execution context."""
    context = dict(_log_context.get())
    for key in _ID_FIELDS:
        value = ids.get(key)
        if value is not None:
            context[key] = str(value)
    return _log_context.set(context)


def reset_log_context(token: Token) -> None:
    _log_context.reset(token)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        span_context = trace.get_current_span().get_span_context()
        otel_trace_id = format(span_context.trace_id, "032x") if span_context.is_valid else None
        otel_span_id = format(span_context.span_id, "016x") if span_context.is_valid else None
        context = _log_context.get()
        log_record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "trace_id": otel_trace_id,
            "span_id": otel_span_id,
            "msg": record.getMessage(),
        }
        for key in _ID_FIELDS:
            value = getattr(record, key, None)
            log_record[key] = str(value) if value is not None else context.get(key)
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)
