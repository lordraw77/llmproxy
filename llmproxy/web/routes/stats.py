"""Statistics / metrics / process-manager endpoints.

- ``GET /stats.json`` — machine-readable metrics + process snapshot (JSON).
- ``GET /stats``      — a self-contained, auto-refreshing HTML dashboard.

The dashboard is a Jinja template (``web/templates/stats.html``); this module
only assembles and shapes the data. Jinja's autoescaping is what keeps the page
safe by construction — the renderer this replaced built the HTML with f-strings
and escaped nothing, which is how a request path became a stored XSS (``F1``).

Both endpoints respect the optional inbound ``PROXY_API_KEY`` like every other
endpoint (they are not in the auth-exempt set). Metrics are per-worker: see
:mod:`llmproxy.metrics`.
"""

from flask import Blueprint, jsonify, render_template

from ...metrics import process_info
from ..container import deps

bp = Blueprint("stats", __name__)

#: How often the dashboard reloads itself, in seconds.
REFRESH_SECONDS = 5


def _payload():
    """Assemble the combined metrics + process snapshot."""
    container = deps()
    metrics = container.metrics.snapshot()
    if container.cache is not None:
        # Surface the response cache alongside the other metric groups.
        metrics["cache"] = container.cache.snapshot()
    if container.audit is not None:
        metrics["audit"] = container.audit.snapshot()
    return {
        "metrics": metrics,
        "process": process_info(container.settings),
        "models": {
            "exposed": list(container.registry.models),
            "default": container.registry.default_model,
            "embeddings": container.registry.embeddings_model,
            "providers": [p.name for p in container.registry.providers],
        },
    }


@bp.route("/stats.json", methods=["GET"])
def stats_json():
    """Return the raw metrics + process snapshot as JSON."""
    return jsonify(_payload())


@bp.route("/stats", methods=["GET"])
def stats_dashboard():
    """Render the HTML metrics / process dashboard (auto-refreshing)."""
    return render_template("stats.html", **_template_context(_payload()))


def _template_context(payload):
    """Flatten a :func:`_payload` snapshot into the names the template binds.

    Ordering and formatting stay here rather than in the template: sorting a
    mapping and rendering a duration are decisions about the data, and they are
    testable without going through a renderer.
    """
    metrics = payload["metrics"]
    requests = metrics["requests"]
    cache = metrics.get("cache")
    return {
        "requests": requests,
        "latency": metrics["latency_ms"],
        "tokens": metrics["tokens"],
        "upstream": metrics["upstream"],
        "cache": cache,
        "hit_rate_pct": round(cache["hit_rate"] * 100, 1) if cache else None,
        # Only worth a card when the trail is on: the counters of a disabled
        # audit are three zeros and a filename nothing writes to.
        "audit": metrics.get("audit") if (metrics.get("audit") or {}).get("enabled") else None,
        "process": payload["process"],
        "models": payload["models"],
        "by_status": _sorted_pairs(requests["by_status"]),
        "by_path": _sorted_pairs(requests["by_path"]),
        "uptime": format_uptime(metrics["uptime_seconds"]),
        "started_at": metrics["started_at"],
        "refresh_seconds": REFRESH_SECONDS,
    }


def _sorted_pairs(mapping):
    """Return ``mapping`` as ``(key, count)`` pairs, most frequent first."""
    return sorted((mapping or {}).items(), key=lambda kv: (-kv[1], kv[0]))


def format_uptime(seconds):
    """Render a duration in seconds as ``NdNhNmNs`` (dropping leading zero units)."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)
