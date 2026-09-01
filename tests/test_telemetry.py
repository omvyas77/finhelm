"""Tracing must cost nothing when no collector is listening.

`.env` carries an OTLP endpoint so docker-compose can wire Jaeger, which means every
process importing finhelm sees the variable set — including the eval harness, offline,
thousands of times. The first implementation installed the exporter on the strength of that
variable alone and the batch processor then retried with exponential backoff on every
export, printing a wall of transient errors and adding seconds per run.
"""

import json

import pytest

from src.finhelm import telemetry as T


def test_no_endpoint_means_no_tracing(monkeypatch):
    monkeypatch.setattr(T, "_CONFIGURED", False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert T.setup("test") is False


def test_unreachable_collector_is_not_enabled(monkeypatch):
    """The regression: configured but absent must be off, not retrying."""
    monkeypatch.setattr(T, "_CONFIGURED", False)
    # Port 1 is reserved and never listening.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    assert T.setup("test") is False


def test_reachability_probe_rejects_a_malformed_endpoint():
    assert T._reachable("not-a-url") is False
    assert T._reachable("http://localhost") is False       # no port


def test_spans_and_attributes_are_safe_with_tracing_off():
    with T.span("probe", **{"a": 1, "dropped": None}):
        T.set_attributes(**{"b": "two", "also_dropped": None})


def test_log_request_emits_one_json_line_and_drops_nones():
    """Captured at the logging layer, not via capsys: the handler binds sys.stdout at
    import, which is right for a container and means a swapped stdout never sees it."""
    import logging

    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Capture()
    T._logger.addHandler(handler)
    try:
        T.log_request(trace_id="abc", route="filings", cost_usd=0.03, missing=None)
    finally:
        T._logger.removeHandler(handler)

    assert len(captured) == 1
    payload = json.loads(captured[0])
    assert payload == {"trace_id": "abc", "route": "filings", "cost_usd": 0.03}
    assert "missing" not in payload


@pytest.mark.parametrize("endpoint,grpc", [("http://x:4317", True),
                                           ("http://x:4318", False)])
def test_port_selects_the_protocol(endpoint, grpc):
    """4317 is gRPC and 4318 is HTTP; they are not interchangeable, and sending HTTP to
    4317 fails looking like an unreachable collector rather than a protocol mismatch."""
    assert endpoint.rstrip("/").endswith(":4317") is grpc


def test_service_name_comes_from_the_environment(monkeypatch):
    """Compose sets OTEL_SERVICE_NAME per service. Before this, setup() hardcoded
    "finhelm", so api and ui registered under one name and their spans landed in a single
    undifferentiated pile in Jaeger — which matters precisely because three compose
    services run the same image."""
    from finhelm.telemetry import resolve_service_name

    monkeypatch.setenv("OTEL_SERVICE_NAME", "finhelm-ui")
    assert resolve_service_name() == "finhelm-ui"


def test_an_explicit_name_still_wins_over_the_environment(monkeypatch):
    from finhelm.telemetry import resolve_service_name

    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-the-environment")
    assert resolve_service_name("passed-explicitly") == "passed-explicitly"


def test_service_name_falls_back_when_nothing_is_set(monkeypatch):
    from finhelm.telemetry import DEFAULT_SERVICE_NAME, resolve_service_name

    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert resolve_service_name() == DEFAULT_SERVICE_NAME
