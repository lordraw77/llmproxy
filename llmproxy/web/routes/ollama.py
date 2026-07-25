"""Ollama-native endpoints (``/`` and ``/api/*``).

Adapts the upstream OpenAI responses into Ollama's NDJSON / JSON dialect.
"""

import json

from flask import Blueprint, Response, g, jsonify, request

from ...upstream.client import resp_json
from ...upstream.sse import iter_nvidia_sse
from ..container import deps
from ..formatting import log_stream_usage, now_iso

bp = Blueprint("ollama", __name__)


@bp.route("/", methods=["GET"])
def root():
    """Liveness root that mimics Ollama's banner string."""
    return "Ollama is running"


@bp.route("/api/version", methods=["GET"])
def version():
    """Return a fake Ollama version string (Ollama ``/api/version`` compatibility)."""
    return jsonify({"version": "0.0.0-llmproxy"})


@bp.route("/api/tags", methods=["GET"])
def tags():
    """List the exposed models in Ollama ``/api/tags`` format."""
    return jsonify({
        "models": [{
            "name": name,
            "model": name,
            "modified_at": now_iso(),
            "size": 0,
            "digest": "llmproxy",
            "details": {
                "format": "api",
                "family": "nvidia",
                "families": None,
                "parameter_size": "",
                "quantization_level": "",
            },
        } for name in deps().registry.models]
    })


@bp.route("/api/show", methods=["POST"])
def show():
    """Return placeholder model metadata in Ollama ``/api/show`` format."""
    return jsonify({
        "license": "",
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {
            "format": "api",
            "family": "nvidia",
            "families": None,
            "parameter_size": "",
            "quantization_level": "",
        },
    })


@bp.route("/api/chat", methods=["POST"])
def chat():
    """Ollama ``/api/chat``: forward a chat request and return NDJSON (stream) or JSON."""
    container = deps()
    body = request.get_json(force=True) or {}
    messages = body.get("messages", [])
    stream = body.get("stream", True)
    options = body.get("options", {})
    model = container.registry.resolve(body.get("model"))
    rid = getattr(g, "req_id", None)

    upstream = container.completions.chat(messages, stream, rid, options, model=model)

    if not stream:
        data = resp_json(upstream)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        result = {
            "model": model,
            "created_at": now_iso(),
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
        }
        if usage:
            result["prompt_eval_count"] = usage.get("prompt_tokens")
            result["eval_count"] = usage.get("completion_tokens")
        return jsonify(result)

    logger = container.logger
    metrics = container.metrics

    def generate():
        """Yield the upstream stream re-framed as Ollama NDJSON chat chunks, then a final done record."""
        # Constant framing computed once: inside the per-token loop we serialize
        # only the content (json.dumps(piece)), avoiding a dict+dumps per chunk.
        created_at = now_iso()
        prefix = '{"model": %s, "created_at": %s, "message": {"role": "assistant", "content": ' % (
            json.dumps(model), json.dumps(created_at),
        )
        suffix = '}, "done": false}\n'
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield prefix + json.dumps(piece) + suffix
        log_stream_usage(logger, metrics, rid, usage)
        done = {
            "model": model,
            "created_at": now_iso(),
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
        }
        if usage:
            done["prompt_eval_count"] = usage.get("prompt_tokens")
            done["eval_count"] = usage.get("completion_tokens")
        yield json.dumps(done) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@bp.route("/api/generate", methods=["POST"])
def generate_endpoint():
    """Ollama ``/api/generate``: prompt (+optional system) completion, NDJSON (stream) or JSON."""
    container = deps()
    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    system = body.get("system")
    stream = body.get("stream", True)
    options = body.get("options", {})
    model = container.registry.resolve(body.get("model"))
    rid = getattr(g, "req_id", None)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    upstream = container.completions.chat(messages, stream, rid, options, model=model)

    if not stream:
        data = resp_json(upstream)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        result = {
            "model": model,
            "created_at": now_iso(),
            "response": content,
            "done": True,
            "done_reason": "stop",
        }
        if usage:
            result["prompt_eval_count"] = usage.get("prompt_tokens")
            result["eval_count"] = usage.get("completion_tokens")
        return jsonify(result)

    logger = container.logger
    metrics = container.metrics

    def generate():
        """Yield the upstream stream re-framed as Ollama NDJSON generate chunks, then a final done record."""
        created_at = now_iso()
        prefix = '{"model": %s, "created_at": %s, "response": ' % (
            json.dumps(model), json.dumps(created_at),
        )
        suffix = ', "done": false}\n'
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield prefix + json.dumps(piece) + suffix
        log_stream_usage(logger, metrics, rid, usage)
        done = {
            "model": model,
            "created_at": now_iso(),
            "response": "",
            "done": True,
            "done_reason": "stop",
        }
        if usage:
            done["prompt_eval_count"] = usage.get("prompt_tokens")
            done["eval_count"] = usage.get("completion_tokens")
        yield json.dumps(done) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@bp.route("/api/embeddings", methods=["POST"])
def api_embeddings():
    """Ollama-style embeddings (legacy): ``{"model", "prompt"}`` -> ``{"embedding": [...]}``."""
    container = deps()
    body = request.get_json(force=True) or {}
    text = body.get("prompt", "")
    model = container.embeddings.resolve_model(body.get("model"))
    rid = getattr(g, "req_id", None)

    payload = container.embeddings.with_input_type({"model": model, "input": text})
    data = resp_json(container.embeddings.embed(payload, rid))
    embedding = (data.get("data") or [{}])[0].get("embedding", [])
    return jsonify({"embedding": embedding})


@bp.route("/api/embed", methods=["POST"])
def api_embed():
    """Ollama-style embeddings (new): ``{"model", "input"}`` -> ``{"embeddings": [[...]]}``."""
    container = deps()
    body = request.get_json(force=True) or {}
    inp = body.get("input", "")
    model = container.embeddings.resolve_model(body.get("model"))
    rid = getattr(g, "req_id", None)

    payload = container.embeddings.with_input_type({"model": model, "input": inp})
    data = resp_json(container.embeddings.embed(payload, rid))
    embeddings = [d.get("embedding", []) for d in (data.get("data") or [])]
    return jsonify({"model": model, "embeddings": embeddings})
