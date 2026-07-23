#!/usr/bin/env python3
"""Server compatibile con le API di Ollama che inoltra le richieste a NVIDIA (OpenAI-compatible)."""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request

load_dotenv()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "11434"))

# --- Logging / telemetria -------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# Timezone dell'orologio nei log. Default: LOG_TZ, poi TZ, poi UTC.
LOG_TZ = os.environ.get("LOG_TZ", os.environ.get("TZ", "UTC"))
try:
    LOG_TZINFO = ZoneInfo(LOG_TZ)
except (ZoneInfoNotFoundError, ValueError):
    LOG_TZINFO = timezone.utc
    LOG_TZ = "UTC"


class TZFormatter(logging.Formatter):
    """Formatter che stampa l'orario nella timezone configurata (LOG_TZ)."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, LOG_TZINFO)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")


_handler = logging.StreamHandler()
_handler.setFormatter(TZFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logger = logging.getLogger("llmproxy")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.handlers = [_handler]
logger.propagate = False

NVIDIA_API_BASE = os.environ.get("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")


def _parse_models(raw, fallback):
    """Interpreta una lista di modelli separati da virgola, con fallback su un singolo modello."""
    models = [m.strip() for m in (raw or "").split(",") if m.strip()]
    return models or [fallback]


# Lista dei modelli esposti (NVIDIA_MODELS separati da virgola). Retro-compatibile con NVIDIA_MODEL.
NVIDIA_MODELS = _parse_models(os.environ.get("NVIDIA_MODELS", NVIDIA_MODEL), NVIDIA_MODEL)
DEFAULT_MODEL = NVIDIA_MODELS[0]


def resolve_model(requested):
    """Restituisce il modello richiesto se e' tra quelli esposti, altrimenti il default."""
    if requested and requested in NVIDIA_MODELS:
        return requested
    return DEFAULT_MODEL


app = Flask(__name__)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def nvidia_headers():
    return {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }


def _request_id():
    """ID breve per correlare i log di una singola richiesta (client <-> upstream)."""
    return getattr(g, "req_id", None) or uuid.uuid4().hex[:8]


def _post_upstream(payload, stream):
    """POST verso NVIDIA con logging dettagliato di richiesta, stato risposta e telemetria."""
    rid = _request_id()
    model = payload.get("model")
    messages = payload.get("messages") or []
    input_chars = sum(len(str(m.get("content", ""))) for m in messages)

    logger.info(
        "[%s] -> NVIDIA request | model=%s stream=%s messages=%d input_chars=%d",
        rid, model, stream, len(messages), input_chars,
    )
    logger.debug("[%s] -> NVIDIA payload: %s", rid, json.dumps(payload)[:2000])

    start = time.perf_counter()
    try:
        resp = requests.post(
            f"{NVIDIA_API_BASE}/chat/completions",
            headers=nvidia_headers(),
            json=payload,
            stream=stream,
            timeout=120,
        )
    except requests.exceptions.RequestException as err:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error("[%s] <- NVIDIA no-response after %.0fms | %s", rid, elapsed_ms, err)
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.INFO if resp.ok else logging.WARNING
    logger.log(
        level,
        "[%s] <- NVIDIA response | status=%s latency=%.0fms stream=%s",
        rid, resp.status_code, elapsed_ms, stream,
    )

    if not resp.ok:
        # Il corpo dell'errore (per un non-2xx) e' piccolo: logghiamolo per capire il perche'.
        logger.warning("[%s] <- NVIDIA error body: %s", rid, resp.text[:1000])
    elif not stream:
        # Telemetria dei token (disponibile solo per le risposte non in streaming).
        try:
            usage = resp.json().get("usage") or {}
        except ValueError:
            usage = {}
        if usage:
            logger.info(
                "[%s] telemetry | prompt_tokens=%s completion_tokens=%s total_tokens=%s latency=%.0fms",
                rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("total_tokens"), elapsed_ms,
            )

    resp.raise_for_status()
    return resp


def call_nvidia(messages, stream, options=None, model=None):
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": stream,
    }
    options = options or {}
    if "temperature" in options:
        payload["temperature"] = options["temperature"]
    if "top_p" in options:
        payload["top_p"] = options["top_p"]

    return _post_upstream(payload, stream)


def call_nvidia_passthrough(payload, stream, model=None):
    """Inoltra un payload gia' in formato OpenAI (usato da /v1/*), forzando solo model/stream."""
    payload = dict(payload)
    payload["model"] = model or DEFAULT_MODEL
    payload["stream"] = stream

    return _post_upstream(payload, stream)


def iter_nvidia_sse(resp):
    """Estrae il testo dei delta da uno stream SSE in formato OpenAI."""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


@app.before_request
def _log_request_start():
    """Assegna un ID di correlazione e logga la richiesta in ingresso."""
    g.req_id = uuid.uuid4().hex[:8]
    g.req_start = time.perf_counter()
    body = request.get_json(silent=True) or {}
    logger.info(
        "[%s] --> %s %s | client=%s model=%s stream=%s",
        g.req_id, request.method, request.path,
        request.remote_addr,
        body.get("model") if isinstance(body, dict) else None,
        body.get("stream") if isinstance(body, dict) else None,
    )


@app.after_request
def _log_request_end(response):
    """Logga esito e durata totale lato client (telemetria end-to-end)."""
    if hasattr(g, "req_start"):
        elapsed_ms = (time.perf_counter() - g.req_start) * 1000
        logger.info(
            "[%s] <-- %s %s | status=%s duration=%.0fms",
            getattr(g, "req_id", "--------"), request.method, request.path,
            response.status_code, elapsed_ms,
        )
    return response


@app.errorhandler(requests.exceptions.RequestException)
def handle_nvidia_error(err):
    """Propaga al client l'errore ricevuto dal provider (status + corpo JSON quando disponibile)."""
    rid = getattr(g, "req_id", "--------")
    upstream = getattr(err, "response", None)

    # Nessuna risposta dall'upstream (timeout, DNS, connessione rifiutata, ...).
    if upstream is None:
        logger.error("[%s] upstream unreachable: %s", rid, err)
        return jsonify({"error": {"message": str(err), "type": "upstream_request_error"}}), 502

    status = upstream.status_code
    logger.warning("[%s] propagating upstream error | status=%s", rid, status)

    # Prova a inoltrare il corpo dell'errore del provider cosi' com'e' (formato OpenAI/NVIDIA).
    try:
        body = upstream.json()
    except ValueError:
        body = {"error": {"message": upstream.text, "type": "upstream_error", "code": status}}

    return jsonify(body), status


@app.route("/", methods=["GET"])
def root():
    return "Ollama is running"


@app.route("/api/version", methods=["GET"])
def version():
    return jsonify({"version": "0.0.0-llmproxy"})


@app.route("/api/tags", methods=["GET"])
def tags():
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
        } for name in NVIDIA_MODELS]
    })


@app.route("/api/show", methods=["POST"])
def show():
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


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True) or {}
    messages = body.get("messages", [])
    stream = body.get("stream", True)
    options = body.get("options", {})
    model = resolve_model(body.get("model"))

    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({
            "model": model,
            "created_at": now_iso(),
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
        })

    def generate():
        for piece in iter_nvidia_sse(upstream):
            yield json.dumps({
                "model": model,
                "created_at": now_iso(),
                "message": {"role": "assistant", "content": piece},
                "done": False,
            }) + "\n"
        yield json.dumps({
            "model": model,
            "created_at": now_iso(),
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
        }) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/api/generate", methods=["POST"])
def generate_endpoint():
    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    system = body.get("system")
    stream = body.get("stream", True)
    options = body.get("options", {})
    model = resolve_model(body.get("model"))

    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({
            "model": model,
            "created_at": now_iso(),
            "response": content,
            "done": True,
            "done_reason": "stop",
        })

    def generate():
        for piece in iter_nvidia_sse(upstream):
            yield json.dumps({
                "model": model,
                "created_at": now_iso(),
                "response": piece,
                "done": False,
            }) + "\n"
        yield json.dumps({
            "model": model,
            "created_at": now_iso(),
            "response": "",
            "done": True,
            "done_reason": "stop",
        }) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/props", methods=["GET"])
def props():
    return jsonify({
        "default_generation_settings": {"model": DEFAULT_MODEL, "n_ctx": 4096},
        "total_slots": 1,
        "model_path": DEFAULT_MODEL,
        "chat_template": "",
    })


@app.route("/v1/models", methods=["GET"])
def v1_models():
    created = int(time.time())
    return jsonify({
        "object": "list",
        "data": [{
            "id": name,
            "object": "model",
            "created": created,
            "owned_by": "nvidia",
        } for name in NVIDIA_MODELS],
    })


@app.route("/v1/chat/completions", methods=["POST"])
def v1_chat_completions():
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    stream = body.get("stream", False)
    model = resolve_model(body.get("model"))
    upstream = call_nvidia_passthrough(body, stream, model=model)

    if not stream:
        data = upstream.json()
        data["model"] = model
        return jsonify(data)

    def relay():
        for chunk in upstream.iter_content(chunk_size=None):
            if chunk:
                yield chunk

    return Response(relay(), mimetype="text/event-stream")


@app.route("/v1/completions", methods=["POST"])
def v1_completions():
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "".join(prompt)
    stream = body.get("stream", False)
    options = {k: body[k] for k in ("temperature", "top_p") if k in body}
    model = resolve_model(body.get("model"))

    messages = [{"role": "user", "content": prompt}]
    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({
            "id": "cmpl-llmproxy",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"text": content, "index": 0, "logprobs": None, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def generate():
        for piece in iter_nvidia_sse(upstream):
            yield "data: " + json.dumps({
                "id": "cmpl-llmproxy",
                "object": "text_completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"text": piece, "index": 0, "logprobs": None, "finish_reason": None}],
            }) + "\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/completion", methods=["POST"])
def llama_completion():
    """Endpoint nativo di llama.cpp (llama-server)."""
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)
    options = {k: body[k] for k in ("temperature", "top_p") if k in body}
    model = resolve_model(body.get("model"))

    messages = [{"role": "user", "content": prompt}]
    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({
            "content": content,
            "model": model,
            "prompt": prompt,
            "stop": True,
            "stopped_eos": True,
            "tokens_predicted": 0,
            "tokens_evaluated": 0,
        })

    def generate():
        for piece in iter_nvidia_sse(upstream):
            yield "data: " + json.dumps({
                "content": piece,
                "model": model,
                "stop": False,
            }) + "\n\n"
        yield "data: " + json.dumps({
            "content": "",
            "model": model,
            "stop": True,
            "stopped_eos": True,
        }) + "\n\n"

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    if not NVIDIA_API_KEY:
        logger.warning("NVIDIA_API_KEY non impostata in .env: le chiamate falliranno.")
    logger.info("llmproxy in ascolto su http://%s:%s", HOST, PORT)
    logger.info("Modelli esposti: %s", ", ".join(NVIDIA_MODELS))
    logger.info("Default: %s | log level=%s | timezone log=%s", DEFAULT_MODEL, LOG_LEVEL, LOG_TZ)
    app.run(host=HOST, port=PORT, threaded=True)
