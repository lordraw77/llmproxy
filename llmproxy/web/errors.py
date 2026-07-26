"""Translation of upstream failures into client-facing HTTP responses."""

import requests
from flask import g, jsonify

from ..providers import resp_json


def register_error_handlers(app):
    """Attach the upstream-error and routing-error handlers to ``app``."""

    @app.errorhandler(ValueError)
    def handle_routing_error(err):
        """Map a routing/capability refusal to a 400 in the OpenAI error format.

        Raised when the client asks for something the configuration cannot serve:
        an unknown embeddings model, embeddings from a provider that has no such
        endpoint (Anthropic, Gemini), or any model at all with an empty catalogue.
        These are client errors, not proxy failures, and used to reach the client
        as Flask's HTML 500 page.

        A non-JSON upstream body also surfaces as a ``ValueError``, but ``requests``
        raises its own ``JSONDecodeError`` subclass of ``RequestException``, which
        Flask matches to the handler below first.
        """
        from .container import deps

        rid = getattr(g, "req_id", "--------")
        deps().logger.warning("[%s] invalid request: %s", rid, err)
        return jsonify({"error": {"message": str(err), "type": "invalid_request_error"}}), 400

    @app.errorhandler(requests.exceptions.RequestException)
    def handle_upstream_error(err):
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
