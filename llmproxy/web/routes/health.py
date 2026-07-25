"""Health / readiness endpoint."""

import requests
from flask import Blueprint, jsonify, request

from ..container import deps

bp = Blueprint("health", __name__)


@bp.route("/health", methods=["GET"])
def health():
    """Basic liveness; with ``?upstream=1`` it also checks NVIDIA reachability.

    Returns:
        A JSON status document. When the upstream check runs and fails, the
        status is ``degraded`` and the HTTP status is 503.
    """
    container = deps()
    registry = container.registry

    info = {
        "status": "ok",
        "providers": len(registry.providers),
        "models": len(registry.models),
        "default_model": registry.default_model,
    }

    if request.args.get("upstream", "").lower() in ("1", "true", "yes"):
        upstreams = {}
        for provider in registry.providers:
            try:
                r = provider.health(timeout=5)
                upstreams[provider.name] = "ok" if r.ok else f"error:{r.status_code}"
                if not r.ok:
                    info["status"] = "degraded"
            except requests.exceptions.RequestException:
                upstreams[provider.name] = "unreachable"
                info["status"] = "degraded"
        info["upstreams"] = upstreams
        return jsonify(info), 200 if info["status"] == "ok" else 503

    return jsonify(info)
