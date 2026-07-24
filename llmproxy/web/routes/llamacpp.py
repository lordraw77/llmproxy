"""llama.cpp-native endpoints (``/props`` and ``/completion``)."""

import json
import time

from flask import Blueprint, Response, g, jsonify, request

from ...domain.sampling import build_sampling_params
from ...upstream.client import resp_json
from ...upstream.sse import iter_nvidia_sse
from ..container import deps
from ..formatting import log_stream_usage, require_upstream_key

bp = Blueprint("llamacpp", __name__)


@bp.route("/props", methods=["GET"])
def props():
    """Return llama.cpp-style server properties (``/props`` compatibility)."""
    default_model = deps().registry.default_model
    return jsonify({
        "default_generation_settings": {"model": default_model, "n_ctx": 4096},
        "total_slots": 1,
        "model_path": default_model,
        "chat_template": "",
    })


@bp.route("/completion", methods=["POST"])
@require_upstream_key
def llama_completion():
    """llama.cpp-native ``/completion`` (llama-server): SSE (stream) or JSON reply."""
    container = deps()
    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)
    # llama.cpp uses n_predict/n_probs etc.; normalize the most common aliases.
    options = build_sampling_params(body)
    if body.get("n_predict") is not None and "max_tokens" not in options:
        options["max_tokens"] = body["n_predict"]
    model = container.registry.resolve(body.get("model"))
    rid = getattr(g, "req_id", None)

    messages = [{"role": "user", "content": prompt}]
    upstream = container.completions.chat(messages, stream, rid, options, model=model)

    if not stream:
        data = resp_json(upstream)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return jsonify({
            "content": content,
            "model": model,
            "prompt": prompt,
            "stop": True,
            "stopped_eos": True,
            "tokens_predicted": usage.get("completion_tokens", 0),
            "tokens_evaluated": usage.get("prompt_tokens", 0),
        })

    logger = container.logger
    metrics = container.metrics

    def generate():
        """Yield the upstream stream re-framed as llama.cpp SSE events, then a final stop event."""
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield "data: " + json.dumps({
                "content": piece,
                "model": model,
                "stop": False,
            }) + "\n\n"
        log_stream_usage(logger, metrics, rid, usage)
        yield "data: " + json.dumps({
            "content": "",
            "model": model,
            "stop": True,
            "stopped_eos": True,
        }) + "\n\n"

    return Response(generate(), mimetype="text/event-stream")
