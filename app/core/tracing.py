"""OpenTelemetry context setup shared by the API and Celery worker.

The SDK provider makes span context valid for W3C propagation. An exporter can
be added when a tracing backend is chosen; it is not required for propagation.
"""

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider


def setup_tracing():
    """Install an SDK provider once unless another provider is already active."""
    provider = trace.get_tracer_provider()
    if type(provider).__name__ == "ProxyTracerProvider":
        trace.set_tracer_provider(TracerProvider())
    return trace.get_tracer_provider()


def instrument_fastapi(app) -> None:
    """Create HTTP server spans used by Celery trace-context injection."""
    FastAPIInstrumentor.instrument_app(app, tracer_provider=setup_tracing())
