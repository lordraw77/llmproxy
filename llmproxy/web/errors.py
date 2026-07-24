"""Translation of upstream failures into client-facing HTTP responses."""

import requests
from flask import g, jsonify

from ..upstream.client import resp_json


def register_error_handlers(app):
    """Attach the upstream-error handler to ``app``."""

    @app.errorhandler(requests.exceptions.RequestException)
    def handle_nvidia_error(err):
        """Propagate the provider error to the client (status + JSON body when available).

        Returns:
            A JSON response tuple: 502 when the upstream is unreachable, otherwise the
            upstream status with its (JSON or text) error body forwarded as-is.
        """
        from .container import deps

        logger = deps().logger
        rid = getattr(g, "req_id", "--------")
        upstream = getattr(err, "response", None)

        # No response from the upstream (timeout, DNS, connection refused, ...).
        if upstream is None:
            logger.error("[%s] upstream unreachable: %s", rid, err)
            return jsonify({"error": {"message": str(err), "type": "upstream_request_error"}}), 502

        status = upstream.status_code
        logger.warning("[%s] propagating upstream error | status=%s", rid, status)

        # Try to forward the provider's error body as-is (OpenAI/NVIDIA format).
        try:
            body = resp_json(upstream)
        except ValueError:
            body = {"error": {"message": upstream.text, "type": "upstream_error", "code": status}}

        return jsonify(body), status
