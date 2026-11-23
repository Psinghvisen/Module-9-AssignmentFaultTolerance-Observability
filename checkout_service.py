"""Checkout Service (port 8081).

Endpoints
  POST /checkout   create an order and charge it through the Payment Service
  GET  /metrics    success/failure counts, error rate, latency stats
  GET  /health     liveness check

Every request gets a request ID (taken from the incoming X-Request-ID header,
or generated). The same ID is forwarded to Payment in the X-Request-ID header
and appears in every log line of both services, so one grep follows a request
end to end.

The call to Payment has a timeout (CHECKOUT_TIMEOUT_SECONDS, default 2s). On
timeout Checkout logs the failure, counts it, and returns 504 to the client -
the service itself keeps running.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import REQUEST_ID_HEADER, JsonLogger, Metrics

PORT = int(os.environ.get("CHECKOUT_PORT", "8081"))
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://localhost:8082")
TIMEOUT_S = float(os.environ.get("CHECKOUT_TIMEOUT_SECONDS", "2"))

log = JsonLogger("checkout")
metrics = Metrics("checkout")


def call_payment(request_id, order_id, amount):
    """Returns (ok, http_status, payload_or_reason)."""
    req = urllib.request.Request(
        f"{PAYMENT_URL}/pay",
        data=json.dumps({"order_id": order_id, "amount": amount}).encode(),
        headers={"Content-Type": "application/json", REQUEST_ID_HEADER: request_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return True, resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
        return False, e.code, payload.get("reason", "payment_error")
    except (socket.timeout, TimeoutError):
        return False, 504, "payment_timeout"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return False, 504, "payment_timeout"
        return False, 503, "payment_unavailable"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, request_id=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if request_id:
            self.send_header(REQUEST_ID_HEADER, request_id)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "checkout"})
        elif self.path == "/metrics":
            snap = metrics.snapshot()
            snap["payment_timeout_s"] = TIMEOUT_S
            self._send(200, snap)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/checkout":
            self._send(404, {"error": "not found"})
            return

        request_id = self.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        start = time.perf_counter()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            body = {}
        order_id = body.get("order_id") or "ord_" + uuid.uuid4().hex[:8]
        amount = body.get("amount", 0)

        log.info("checkout_received", request_id=request_id, order_id=order_id, amount=amount)
        log.info("calling_payment", request_id=request_id, order_id=order_id,
                 url=f"{PAYMENT_URL}/pay", timeout_s=TIMEOUT_S)

        ok, status, result = call_payment(request_id, order_id, amount)
        latency = (time.perf_counter() - start) * 1000

        if ok:
            metrics.record(True, latency)
            log.info("checkout_completed", request_id=request_id, order_id=order_id,
                     payment_id=result.get("payment_id"), status=200,
                     latency_ms=round(latency, 2))
            self._send(200, {"status": "success", "order_id": order_id,
                             "payment_id": result.get("payment_id"),
                             "request_id": request_id,
                             "latency_ms": round(latency, 2)}, request_id)
            return

        metrics.record(False, latency, reason=result)
        if result == "payment_timeout":
            log.error("payment_timeout", request_id=request_id, order_id=order_id,
                      timeout_s=TIMEOUT_S, status=status, latency_ms=round(latency, 2),
                      detail=f"Payment Service did not respond within {TIMEOUT_S}s")
        else:
            log.error("checkout_failed", request_id=request_id, order_id=order_id,
                      reason=result, status=status, latency_ms=round(latency, 2))
        self._send(status, {"status": "failed", "order_id": order_id, "reason": result,
                            "request_id": request_id,
                            "latency_ms": round(latency, 2)}, request_id)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("service_started", port=PORT, payment_url=PAYMENT_URL, timeout_s=TIMEOUT_S)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("service_stopped")
