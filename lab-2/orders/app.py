import json
import logging
import time
from datetime import datetime, timezone
from threading import Lock, Thread
from urllib.request import urlopen

from flask import Flask, Response, g, request, send_from_directory
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("orders")
FlaskInstrumentor().instrument_app(app, excluded_urls="/metrics,/health,/alerts")

requests_total = Counter("orders_requests", "Completed order API requests")
errors_total = Counter("orders_errors", "Order API responses with status 5xx")
duration = Histogram(
    "orders_duration_seconds", "Order API response time",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5),
)
events = logging.getLogger("orders.events")
events.setLevel(logging.INFO)
events.addHandler(logging.FileHandler("/logs/orders.jsonl"))
events.propagate = False
load_lock = Lock()


def log_event(level, message, **fields):
    context = trace.get_current_span().get_span_context()
    events.info(json.dumps({
        "time": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
        **fields,
    }))


@app.before_request
def start_timer():
    g.started = time.monotonic()


@app.after_request
def record_result(response):
    if request.path.startswith("/api/orders"):
        requests_total.inc()
        duration.observe(time.monotonic() - g.started)
        failed = response.status_code >= 500
        if failed:
            errors_total.inc()
            trace.get_current_span().set_status(Status(StatusCode.ERROR))
        level, message = ("error", "Order request failed") if failed else ("info", "Order request completed")
        log_event(level, message, path=request.path, status=response.status_code)
    return response


@app.get("/")
def home():
    return send_from_directory(app.root_path, "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@app.get("/api/orders")
def orders():
    return {"orders": []}


@app.post("/api/orders/error")
def order_error():
    return {"message": "The order could not be saved"}, 503


@app.post("/api/orders/slow")
def slow_order():
    with tracer.start_as_current_span("slow-dependency"):
        time.sleep(2)
    return {"message": "Order lookup completed after 2 seconds"}


def request_batch():
    try:
        for _ in range(60):
            with urlopen("http://127.0.0.1:8080/api/orders", timeout=4) as response:
                response.read()
            time.sleep(0.04)
    except OSError as exc:
        log_event("error", "Load batch interrupted", reason=str(exc))
    finally:
        load_lock.release()


@app.post("/api/orders/load")
def generate_load():
    if not load_lock.acquire(blocking=False):
        return {"message": "A batch is already running"}, 409
    Thread(target=request_batch, daemon=True).start()
    return {"message": "Started 60 local order requests"}, 202


@app.post("/alerts")
def receive_alerts():
    for alert in request.get_json()["alerts"]:
        log_event("info", "Alertmanager notification",
                  alert=alert["labels"]["alertname"], state=alert["status"])
    return "", 204
