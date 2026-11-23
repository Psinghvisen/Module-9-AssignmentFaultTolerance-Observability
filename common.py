"""Shared observability helpers: structured JSON logging + in-memory metrics.

Standard library only, so the services run with a plain `python3`.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

REQUEST_ID_HEADER = "X-Request-ID"


class JsonLogger:
    """Writes one JSON object per line to stdout and to logs/<service>.log."""

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._file = open(os.path.join(LOG_DIR, f"{service}.log"), "a", buffering=1)

    def log(self, level, event, **fields):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "service": self.service,
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(record)
        with self._lock:
            print(line, file=sys.stdout, flush=True)
            self._file.write(line + "\n")

    def info(self, event, **fields):
        self.log("INFO", event, **fields)

    def warn(self, event, **fields):
        self.log("WARN", event, **fields)

    def error(self, event, **fields):
        self.log("ERROR", event, **fields)


class Metrics:
    """Thread-safe counters and latency stats.

    Keeps lifetime totals plus a sliding window of the most recent requests so
    the error rate and latency visibly change during failure and recovery.
    """

    WINDOW = 10  # number of most recent requests used for "recent" stats

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self.started = time.time()
        self.total = 0
        self.success = 0
        self.failure = 0
        self.failure_reasons = {}
        self.latencies_ms = []
        self.recent = []  # list of (ok: bool, latency_ms: float)

    def record(self, ok, latency_ms, reason=None):
        with self._lock:
            self.total += 1
            if ok:
                self.success += 1
            else:
                self.failure += 1
                key = reason or "error"
                self.failure_reasons[key] = self.failure_reasons.get(key, 0) + 1
            self.latencies_ms.append(latency_ms)
            self.recent.append((ok, latency_ms))
            self.recent = self.recent[-self.WINDOW:]

    @staticmethod
    def _pct(values, p):
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
        return round(s[idx], 2)

    def snapshot(self):
        with self._lock:
            lat = list(self.latencies_ms)
            recent = list(self.recent)
            recent_lat = [l for _, l in recent]
            recent_fail = sum(1 for ok, _ in recent if not ok)
            return {
                "service": self.service,
                "uptime_s": round(time.time() - self.started, 1),
                "requests_total": self.total,
                "requests_success": self.success,
                "requests_failure": self.failure,
                "failure_reasons": dict(self.failure_reasons),
                "error_rate": round(self.failure / self.total, 3) if self.total else 0.0,
                "latency_ms": {
                    "avg": round(sum(lat) / len(lat), 2) if lat else 0.0,
                    "p50": self._pct(lat, 50),
                    "p95": self._pct(lat, 95),
                    "max": round(max(lat), 2) if lat else 0.0,
                },
                f"last_{self.WINDOW}": {
                    "requests": len(recent),
                    "error_rate": round(recent_fail / len(recent), 3) if recent else 0.0,
                    "avg_latency_ms": round(sum(recent_lat) / len(recent_lat), 2) if recent_lat else 0.0,
                },
            }
