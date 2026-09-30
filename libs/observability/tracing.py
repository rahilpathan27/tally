"""OpenTelemetry tracing and log correlation.

Tracing is enabled when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; spans cover inbound FastAPI
requests, outbound httpx calls (so a payment trace follows core → risk → ledger → simulators)
and asyncpg queries. ``CorrelationFilter`` adds the active trace/span IDs and any bound business
identifiers (merchant, payment, entry) to every log record.
"""

from __future__ import annotations

import contextvars
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_CONTEXT: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "tally_log_context", default=None
)
_CONFIGURED = False


@contextmanager
def bind(**fields: str) -> Iterator[None]:
    """Attach identifiers (payment_id, merchant_id, ...) to logs and the current span."""
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **fields})
    span = trace.get_current_span()
    for key, value in fields.items():
        span.set_attribute(f"tally.{key}", value)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            record.trace_id = f"{context.trace_id:032x}"
            record.span_id = f"{context.span_id:016x}"
        for key, value in (_CONTEXT.get() or {}).items():
            setattr(record, key, value)
        return True


def configure_tracing(service: str, app: Any | None = None) -> bool:
    """Install the OTLP exporter and instrumentations once per process."""
    global _CONFIGURED
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, CorrelationFilter) for f in handler.filters):
            handler.addFilter(CorrelationFilter())
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    if not _CONFIGURED:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]
        _CONFIGURED = True
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/.*,metrics")
    return True
