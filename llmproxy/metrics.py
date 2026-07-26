"""In-process metrics collection and a process/runtime snapshot.

A single thread-safe :class:`MetricsCollector` per worker accumulates request,
latency, token, and upstream counters in memory. It has no external dependencies
and no persistence — it is a lightweight, always-on observability surface exposed
by the ``/stats`` endpoints.

Latency covers the whole client-side duration, streaming included: streaming
routes hand their accounting to the response generator (see
:class:`~llmproxy.web.middleware.DeferredMetrics`), so a request is recorded when
its last frame leaves and stays counted in ``in_flight`` until then.

Note on multi-worker deployments: under gunicorn each worker is a separate
process with its own collector, so a single ``/stats`` response reflects only the
worker that served it. For aggregated, cross-worker metrics use an external
scraper (e.g. Prometheus + a shared store). :func:`process_info` reports the
serving worker's PID and resource usage precisely for this reason.
"""

import os
import platform
import resource
import sys
import threading
import time
from datetime import datetime, timezone


class MetricsCollector:
    """Thread-safe, in-memory accumulator of request/latency/token/upstream counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start = time.time()

        # Request counters.
        self.total_requests = 0
        self.in_flight = 0
        self.errors = 0
        self.by_status = {}   # "200" -> count
        self.by_path = {}     # "/api/chat" -> count

        # Client-side latency (ms).
        self.lat_count = 0
        self.lat_sum = 0.0
        self.lat_max = 0.0

        # Token usage relayed from the upstream.
        self.tok_prompt = 0
        self.tok_completion = 0
        self.tok_total = 0

        # Upstream call telemetry.
        self.up_calls = 0
        self.up_errors = 0
        self.up_lat_sum = 0.0
        self.up_lat_max = 0.0

    # -- recording -----------------------------------------------------------

    def begin(self):
        """Mark a request as in-flight (gauge increment)."""
        with self._lock:
            self.in_flight += 1

    def end(self):
        """Mark a request as finished (gauge decrement)."""
        with self._lock:
            if self.in_flight > 0:
                self.in_flight -= 1

    def record(self, path, status, duration_ms):
        """Record a completed request's path, status code, and client-side duration."""
        with self._lock:
            self.total_requests += 1
            self.by_path[path] = self.by_path.get(path, 0) + 1
            key = str(status)
            self.by_status[key] = self.by_status.get(key, 0) + 1
            if status >= 400:
                self.errors += 1
            self.lat_count += 1
            self.lat_sum += duration_ms
            if duration_ms > self.lat_max:
                self.lat_max = duration_ms

    def record_tokens(self, usage):
        """Accumulate prompt/completion/total tokens from an upstream ``usage`` object."""
        if not usage:
            return
        with self._lock:
            self.tok_prompt += usage.get("prompt_tokens") or 0
            self.tok_completion += usage.get("completion_tokens") or 0
            self.tok_total += usage.get("total_tokens") or 0

    def record_upstream(self, duration_ms, ok):
        """Record one upstream call: its latency and whether it succeeded."""
        with self._lock:
            self.up_calls += 1
            if not ok:
                self.up_errors += 1
            self.up_lat_sum += duration_ms
            if duration_ms > self.up_lat_max:
                self.up_lat_max = duration_ms

    # -- reporting -----------------------------------------------------------

    def snapshot(self):
        """Return a JSON-serializable snapshot of the accumulated metrics."""
        with self._lock:
            uptime = time.time() - self._start
            avg_lat = (self.lat_sum / self.lat_count) if self.lat_count else 0.0
            up_avg = (self.up_lat_sum / self.up_calls) if self.up_calls else 0.0
            return {
                "started_at": datetime.fromtimestamp(self._start, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "uptime_seconds": round(uptime, 1),
                "requests": {
                    "total": self.total_requests,
                    "in_flight": self.in_flight,
                    "errors": self.errors,
                    "by_status": dict(self.by_status),
                    "by_path": dict(self.by_path),
                },
                "latency_ms": {
                    "avg": round(avg_lat, 1),
                    "max": round(self.lat_max, 1),
                    "count": self.lat_count,
                },
                "tokens": {
                    "prompt": self.tok_prompt,
                    "completion": self.tok_completion,
                    "total": self.tok_total,
                },
                "upstream": {
                    "calls": self.up_calls,
                    "errors": self.up_errors,
                    "avg_latency_ms": round(up_avg, 1),
                    "max_latency_ms": round(self.up_lat_max, 1),
                },
            }


def _max_rss_mb():
    """Return the peak resident-set size of this process in MiB.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS; we assume the Linux
    convention (the supported runtime), which matches the Docker image.
    """
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(kb / 1024, 1)


def process_info(settings):
    """Return a snapshot of the serving worker's process / runtime environment.

    Reflects the gunicorn "process manager" view: which worker (PID) answered,
    how the pool is configured (``WEB_CONCURRENCY`` workers x ``THREADS``), how
    long it has been up, and its memory footprint.
    """
    return {
        "pid": os.getpid(),
        "server": "gunicorn" if "gunicorn" in sys.modules else "flask-dev",
        "workers_configured": int(os.environ.get("WEB_CONCURRENCY", "2")),
        "threads_per_worker": int(os.environ.get("THREADS", str(settings.pool_size))),
        "worker_timeout_seconds": int(os.environ.get("GUNICORN_TIMEOUT", "600")),
        "memory_rss_mb": _max_rss_mb(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
