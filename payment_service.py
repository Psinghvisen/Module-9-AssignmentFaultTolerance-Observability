"""Payment Service (port 8082).

Endpoints
  POST /pay           process a payment (honours X-Request-ID from Checkout)
  GET  /metrics       success/failure counts and latency stats
  GET  /health        liveness check
  GET  /admin/delay   show the current injected delay
  POST /admin/delay   set the injected delay, body: {"seconds": 5}

The delay is used for the failure test. It can be set at startup with the
PAYMENT_DELAY_SECONDS env var, or changed at runtime with POST /admin/delay.
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import REQUEST_ID_HEADER, JsonLogger, Metrics

PORT = int(os.environ.get("PAYMENT_PORT", "8082"))

log = JsonLogger("payment")
metrics = Metrics("payment")
state = {"delay_seconds": float(os.environ.get("PAYMENT_DELAY_SECONDS", "0"))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default access log; we log JSON
        pass

    def _send(self, status, body, request_id=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if request_id:
            self.send_header(REQUEST_ID_HEADER, request_id)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Caller (Checkout) already gave up because of its timeout.
            log.warn("client_disconnected", request_id=request_id,
                     detail="caller closed connection before response was sent")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "payment"})
        elif self.path == "/metrics":
            self._send(200, metrics.snapshot())
        elif self.path == "/admin/delay":
            self._send(200, state)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/admin/delay":
            body = self._body()
            old = state["delay_seconds"]
            state["delay_seconds"] = float(body.get("seconds", 0))
            log.warn("delay_changed", old_delay_s=old, new_delay_s=state["delay_seconds"])
            self._send(200, state)
            return

        if self.path != "/pay":
            self._send(404, {"error": "not found"})
            return

        request_id = self.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        start = time.perf_counter()
        body = self._body()
        order_id = body.get("order_id")
        amount = body.get("amount")
        log.info("payment_received", request_id=request_id, order_id=order_id, amount=amount)

        delay = state["delay_seconds"]
        if delay > 0:
            log.warn("injected_delay", request_id=request_id, delay_s=delay)
            time.sleep(delay)

        if not isinstance(amount, (int, float)) or amount <= 0:
            latency = (time.perf_counter() - start) * 1000
            metrics.record(False, latency, reason="invalid_amount")
            log.error("payment_rejected", request_id=request_id, order_id=order_id,
                      reason="invalid_amount", latency_ms=round(latency, 2))
            self._send(400, {"status": "rejected", "reason": "invalid_amount",
                             "request_id": request_id}, request_id)
            return

        payment_id = "pay_" + uuid.uuid4().hex[:10]
        latency = (time.perf_counter() - start) * 1000
        metrics.record(True, latency)
        log.info("payment_completed", request_id=request_id, order_id=order_id,
                 payment_id=payment_id, status=200, latency_ms=round(latency, 2))
        self._send(200, {"status": "approved", "payment_id": payment_id,
                         "order_id": order_id, "request_id": request_id}, request_id)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("service_started", port=PORT, delay_s=state["delay_seconds"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("service_stopped")
