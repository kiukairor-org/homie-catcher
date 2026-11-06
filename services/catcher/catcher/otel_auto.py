"""OpenTelemetry zero-code auto-instrumentation (D81, owner decision 2026-11-06: official SDKs and instrumentation
libraries wherever they exist; supersedes D40's "no SDK" rule for the catcher).

``opentelemetry-distro``'s programmatic entry point (the same code ``opentelemetry-instrument`` runs) loads every
installed instrumentation: aiohttp-server (the button callbacks and alert ingest: SERVER spans +
``http.server.request.duration``, which New Relic's golden metrics are computed from) and aiohttp-client (GitHub,
Plane, Mattermost, Kubernetes, NerdGraph: CLIENT spans + ``http.client.request.duration``, the external services).
Called in-process rather than through the ``opentelemetry-instrument`` wrapper so no PYTHONPATH/sitecustomize leaks
into the ``claude`` subprocesses or the Plane MCP server.

The pipeline itself (items, stages, workflow runs, deploys) stays in ``telemetry.py``: those are business events that
last minutes to days, not calls a library can wrap. Off when OTEL_EXPORTER_OTLP_ENDPOINT is unset (tests, local)."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

DEFAULTS = {
    "OTEL_SERVICE_NAME": "homie-catcher",
    "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=homie-pipeline,ai-pipeline=true,k8s.cluster.name=oracle-k3s",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "none",  # pod logs already reach New Relic through otel-logs (D75)
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",  # New Relic's recommendation
    "OTEL_SEMCONV_STABILITY_OPT_IN": "http",  # stable http.server.request.duration (seconds), not http.server.duration
    "OTEL_PYTHON_AIOHTTP_SERVER_EXCLUDED_URLS": "healthz",
    # the catcher's own pipeline exports to the collector would otherwise each become a client span
    "OTEL_PYTHON_AIOHTTP_CLIENT_EXCLUDED_URLS": "otel-collector",
}


def setup() -> bool:
    """Instrument the process; call before any aiohttp ClientSession or web.Application is created."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return False
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    try:
        from opentelemetry.instrumentation.auto_instrumentation import initialize

        initialize()
    except Exception:  # noqa: BLE001 - telemetry must never stop the catcher
        log.exception("OpenTelemetry auto-instrumentation failed; continuing without it")
        return False
    log.info("OpenTelemetry auto-instrumentation on (service %s)", os.environ["OTEL_SERVICE_NAME"])
    return True
