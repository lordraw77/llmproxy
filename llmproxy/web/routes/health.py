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
    settings = container.settings
    registry = container.registry

    info = {
        "status": "ok",
        "api_key_configured": bool(settings.nvidia_api_key),
        "models": len(registry.models),
        "default_model": registry.default_model,
    }

    if request.args.get("upstream", "").lower() in ("1", "true", "yes"):
        try:
            r = container.upstream.get("/models", timeout=5)
            info["upstream"] = "ok" if r.ok else f"error:{r.status_code}"
            if not r.ok:
                info["status"] = "degraded"
        except requests.exceptions.RequestException:
            info["upstream"] = "unreachable"
            info["status"] = "degraded"
        return jsonify(info), 200 if info["status"] == "ok" else 503

    return jsonify(info)
