"""OpenAI-compatible endpoints (``/v1/*``)."""

import json
import time

from flask import Blueprint, g, jsonify, request

from ... import audit
from ...domain.sampling import build_sampling_params
from ...providers import resp_json
from ..container import deps
from ..formatting import completion_json, completion_stream, model_entry, streaming_response

bp = Blueprint("openai", __name__)


@bp.route("/v1/models", methods=["GET"])
@bp.route("/models", methods=["GET"])
def v1_models():
    """OpenAI ``/v1/models``: list the exposed models.

    Also served without the ``/v1`` prefix for clients configured with a base
    URL that omits it.
    """
    created = int(time.time())
    registry = deps().registry
    return jsonify({
        "object": "list",
        "data": [
            model_entry(name, created, registry.owner_of(name)) for name in registry.models
        ],
    })


@bp.route("/v1/models/<path:model_id>", methods=["GET"])
@bp.route("/models/<path:model_id>", methods=["GET"])
def v1_model(model_id):
    """OpenAI ``/v1/models/<id>``: detail of a single model (some OpenAI SDKs query it).

    Returns:
        The model entry, or a 404 JSON error if the model is not exposed.
    """
    registry = deps().registry
    if not registry.has(model_id):
        return jsonify({"error": {"message": f"model '{model_id}' not found", "type": "not_found"}}), 404
    return jsonify(model_entry(model_id, int(time.time()), registry.owner_of(model_id)))


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
        """Relay the upstream SSE bytes to the client unchanged.

        Unchanged includes ``model``: the chunks carry the provider-native id
        while the non-streaming branch above reports the exposed name. Rewriting
        it would mean parsing and re-serializing every chunk. Documented as a
        known limit of the relay in ``docs/api-reference.md`` (F11).

        The audit trail buffers the bytes rather than decoding them here, for the
        same reason: on this route the proxy never parses the stream, and it is
        not going to start doing so per chunk to fill a log. The writer thread
        decodes the buffer once the request is over.
        """
        event = audit.current()
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    event.first_output()
                    event.add_raw(chunk)
                    yield chunk
        finally:
            upstream.close()

    return streaming_response(relay, "text/event-stream")


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
        return completion_json(upstream, lambda content, usage: {
            "id": "cmpl-llmproxy",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"text": content, "index": 0, "logprobs": None, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    # Constant framing computed once, as the Ollama routes already do: inside the
    # per-token loop only the text is serialized, instead of rebuilding and
    # re-serializing a four-level dict for every token. It also pins ``created``
    # to the start of the completion — calling ``time.time()`` per chunk let the
    # field drift across a single response.
    prefix = ('data: {"id": "cmpl-llmproxy", "object": "text_completion", '
              '"created": %d, "model": %s, "choices": [{"text": ') % (
        int(time.time()), json.dumps(model),
    )
    suffix = ', "index": 0, "logprobs": null, "finish_reason": null}]}\n\n'

    def chunk(piece):
        return prefix + json.dumps(piece) + suffix

    return completion_stream(
        upstream, "text/event-stream", chunk, lambda _usage: "data: [DONE]\n\n",
        container.logger, container.metrics, rid,
    )
