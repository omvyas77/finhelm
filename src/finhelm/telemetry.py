"""Tracing and structured logging.

Two separate concerns that are easy to conflate. Spans answer "where did the 8 seconds
go?" and are exported to Jaeger; the JSON log line answers "what did this request
actually do?" and survives whether or not a collector is running. Both carry the same
trace id, so a slow trace in the Jaeger UI can be joined to the line recording which
chunks were retrieved and what it cost.

Everything degrades to a no-op when no collector is configured. That is deliberate: the
eval harness imports the same code paths as the service and runs thousands of times
offline, and instrumentation that raised — or worse, blocked on a connection timeout to a
Jaeger that is not there — would make tracing a liability rather than a tool.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator

_CONFIGURED = False


def _reachable(endpoint: str, timeout: float = 0.25) -> bool:
    """Is something actually listening? Checked before the exporter is installed.

    Without this, a configured-but-absent collector is worse than no configuration at all:
    the batch processor retries with exponential backoff on every export, so each run pays
    seconds of connection failures and prints a wall of transient errors. `.env` carries an
    endpoint for compose, so every process that imports this module — including the eval
    harness, thousands of times offline — would inherit that cost with no collector
    running. One 250 ms probe at startup turns it back into a genuine no-op.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


DEFAULT_SERVICE_NAME = "finhelm"


def resolve_service_name(explicit: str | None = None) -> str:
    """Explicit argument, then OTEL_SERVICE_NAME, then the default.

    A separate function because it is the part worth testing, and testing it through
    setup() means standing up a TracerProvider and stubbing opentelemetry.sdk.resources —
    which breaks that package's own imports.
    """
    return explicit or os.getenv("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME


def setup(service_name: str | None = None) -> bool:
    """Point the tracer at a collector if one is reachable. Returns whether it is on.

    Reads OTEL_EXPORTER_OTLP_ENDPOINT, the standard variable, so compose wires this without
    the code knowing anything about Jaeger specifically.

    The service name comes from OTEL_SERVICE_NAME for the same reason. Compose has been
    setting it per service since the stack was written and nothing read it: the name was
    hardcoded, so api and ui both registered as "finhelm" and their spans landed in one
    undifferentiated pile in Jaeger — which is precisely what a service name is for when
    three containers run the same image.

    The protocol is chosen by port because the two OTLP ports are not interchangeable:
    4317 is gRPC and 4318 is HTTP. Sending HTTP to 4317 fails in a way that looks like an
    unreachable collector rather than a protocol mismatch, which is how this was wired
    wrongly the first time.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return True
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or not _reachable(endpoint):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if endpoint.rstrip("/").endswith(":4317"):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter)
            exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True, timeout=2)
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter)
            exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces",
                                        timeout=2)

        provider = TracerProvider(
            resource=Resource.create({"service.name": resolve_service_name(service_name)}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        return True
    except Exception as exc:  # a broken collector must not take the service with it
        logging.getLogger("finhelm").warning("tracing disabled: %s", exc)
        return False


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span, or nothing at all when tracing is off.

    Attributes are set individually rather than passed to start_as_current_span so that a
    None — an unrouted collection, a missing token count — is skipped instead of raising
    inside the instrumentation.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer("finhelm")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def set_attributes(**attributes: Any) -> None:
    """Add attributes to whatever span is currently active, if any."""
    try:
        from opentelemetry import trace
    except ImportError:
        return
    current = trace.get_current_span()
    if current is None:
        return
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)


_logger = logging.getLogger("finhelm.request")
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log_request(**fields: Any) -> None:
    """One JSON line per request.

    Chunk *ids* rather than chunk text: the point is to be able to reconstruct what
    retrieval returned for a request that went wrong, and the text is recoverable from the
    id. Logging the passages themselves would put megabytes of filing prose into the log
    stream for every question.
    """
    _logger.info(json.dumps({k: v for k, v in fields.items() if v is not None},
                            default=str))
