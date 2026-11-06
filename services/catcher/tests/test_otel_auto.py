"""D81: zero-code auto-instrumentation gives SERVER spans + http.server.request.duration (New Relic golden metrics)
and CLIENT spans for outbound calls, with /healthz and the collector excluded. Runs in a subprocess: instrumentation
patches aiohttp process-wide."""

import os
import subprocess
import sys
import textwrap

from catcher import otel_auto

SCRIPT = textwrap.dedent(
    """
    import asyncio, os
    from catcher import otel_auto
    assert otel_auto.setup()
    from aiohttp import ClientSession, web
    from opentelemetry import metrics, trace

    async def main():
        app = web.Application()
        async def ok(_r): return web.json_response({"ok": True})
        app.router.add_get("/healthz", ok)
        app.router.add_post("/actions/{name}", ok)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as s:
            for path in ("/actions/approve", "/healthz"):
                async with s.request("POST" if "actions" in path else "GET", f"http://127.0.0.1:{port}{path}") as r:
                    await r.read()
        await runner.cleanup()
    asyncio.run(main())
    trace.get_tracer_provider().force_flush(); metrics.get_meter_provider().force_flush()
    """
)


def test_off_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert otel_auto.setup() is False


def test_server_and_client_spans_and_semconv_metric():
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
        "OTEL_TRACES_EXPORTER": "console",
        "OTEL_METRICS_EXPORTER": "console",
        "PYTHONPATH": os.getcwd(),
    }
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    o = out.stdout
    assert '"name": "POST /actions/{name}"' in o and '"kind": "SpanKind.SERVER"' in o, o[:2000]
    assert '"http.route": "/actions/{name}"' in o
    assert '"kind": "SpanKind.CLIENT"' in o
    assert "http.server.request.duration" in o and "http.client.request.duration" in o
    assert "GET /healthz" not in o, "healthz must not be traced"
