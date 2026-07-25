"""OpenAI-compatible endpoints (``/v1/*``)."""

import json
import time

from flask import Blueprint, Response, g, jsonify, request

from ...domain.sampling import build_sampling_params
from ...upstream.client import resp_json
from ...upstream.sse import iter_nvidia_sse
from ..container import deps
from ..formatting import log_stream_usage, model_entry

bp = Blueprint("openai", __name__)


@bp.route("/v1/models", methods=["GET"])
@bp.route("/models", methods=["GET"])
def v1_models():
    """OpenAI ``/v1/models``: list the exposed models.

    Also served without the ``/v1`` prefix for clients configured with a base
    URL that omits it.
    """
    created = int(time.time())
    return jsonify({
        "object": "list",
        "data": [model_entry(name, created) for name in deps().registry.models],
    })


@bp.route("/v1/models/<path:model_id>", methods=["GET"])
@bp.route("/models/<path:model_id>", methods=["GET"])
def v1_model(model_id):
    """OpenAI ``/v1/models/<id>``: detail of a single model (some OpenAI SDKs query it).

    Returns:
        The model entry, or a 404 JSON error if the model is not exposed.
    """
    if not deps().registry.has(model_id):
        return jsonify({"error": {"message": f"model '{model_id}' not found", "type": "not_found"}}), 404
    return jsonify(model_entry(model_id, int(time.time())))


@bp.route("/v1/chat/completions", methods=["POST"])
@bp.route("/chat/completions", methods=["POST"])
def v1_chat_completions():
    """OpenAI ``/v1/chat/completions``: passthrough chat, raw SSE relay when streaming.

    Also served at ``/chat/completions`` (no ``/v1``) for clients whose base URL
    omits the prefix.
    """
    container = deps()
    body = request.get_json(force=True) or {}
    stream = body.get("stream", False)
    model = container.registry.resolve(body.get("model"))
    rid = getattr(g, "req_id", None)
    upstream = container.completions.passthrough(body, stream, rid, model=model)

    if not stream:
        data = resp_json(upstream)
        data["model"] = model
        return jsonify(data)

    def relay():
        """Relay the upstream SSE bytes to the client unchanged."""
        for chunk in upstream.iter_content(chunk_size=None):
            if chunk:
                yield chunk

    return Response(relay(), mimetype="text/event-stream")


@bp.route("/v1/embeddings", methods=["POST"])
@bp.route("/embeddings", methods=["POST"])
def v1_embeddings():
    """OpenAI ``/v1/embeddings``: passthrough to the upstream ``/embeddings`` endpoint."""
    container = deps()
    body = request.get_json(force=True) or {}
    rid = getattr(g, "req_id", None)
    payload = dict(body)
    payload["model"] = container.embeddings.resolve_model(body.get("model"))
    payload = container.embeddings.with_input_type(payload)

    data = resp_json(container.embeddings.embed(payload, rid))
    data["model"] = payload["model"]
    return jsonify(data)


@bp.route("/v1/completions", methods=["POST"])
@bp.route("/completions", methods=["POST"])
def v1_completions():
    """OpenAI ``/v1/completions``: text completion, SSE (stream) or JSON."""
    container = deps()
    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "".join(prompt)
    stream = body.get("stream", False)
    options = build_sampling_params(body)
    model = container.registry.resolve(body.get("model"))
    rid = getattr(g, "req_id", None)

    messages = [{"role": "user", "content": prompt}]
    upstream = container.completions.chat(messages, stream, rid, options, model=model)

    if not stream:
        data = resp_json(upstream)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return jsonify({
            "id": "cmpl-llmproxy",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"text": content, "index": 0, "logprobs": None, "finish_reason": "stop"}],
            "usage": usage,
        })

    logger = container.logger
    metrics = container.metrics

    def generate():
        """Yield the upstream stream re-framed as OpenAI ``text_completion`` SSE events, then ``[DONE]``."""
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield "data: " + json.dumps({
                "id": "cmpl-llmproxy",
                "object": "text_completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"text": piece, "index": 0, "logprobs": None, "finish_reason": None}],
            }) + "\n\n"
        log_stream_usage(logger, metrics, rid, usage)
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")
