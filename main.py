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

# Timeout (secondi) delle chiamate verso l'upstream.
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "120"))

# --- Retry sugli errori transitori dell'upstream --------------------------
# Numero di ritentativi (oltre al primo tentativo) su errori di rete o
# status 429/5xx, con backoff esponenziale. RETRY_MAX=0 disabilita i retry.
RETRY_MAX = int(os.environ.get("RETRY_MAX", "2"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "0.5"))
RETRY_STATUS = {429, 500, 502, 503, 504}

# --- Autenticazione in ingresso (opzionale) -------------------------------
# Se PROXY_API_KEY e' impostata, ogni richiesta (tranne i percorsi esenti)
# deve presentarla via header "Authorization: Bearer <key>" o "X-Api-Key".
# Se vuota, il proxy resta aperto (comportamento storico).
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "").strip()
# Percorsi sempre raggiungibili senza autenticazione (liveness/health-check).
AUTH_EXEMPT_PATHS = {"/", "/health"}

# --- Embeddings -----------------------------------------------------------
# Modello usato per gli embeddings quando il client non ne specifica uno
# (i modelli di chat non sono validi per /embeddings).
NVIDIA_EMBEDDINGS_MODEL = os.environ.get("NVIDIA_EMBEDDINGS_MODEL", "nvidia/nv-embedqa-e5-v5")
# input_type applicato se il client non lo fornisce (molti embedder NVIDIA lo esigono).
EMBEDDINGS_INPUT_TYPE = os.environ.get("EMBEDDINGS_INPUT_TYPE", "query").strip()


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


def resolve_embeddings_model(requested):
    """Per gli embeddings usa il modello richiesto cosi' com'e', altrimenti il default dedicato."""
    return requested or NVIDIA_EMBEDDINGS_MODEL


# Parametri di sampling accettati dall'API OpenAI di NVIDIA (stesso nome in ingresso/uscita).
_SAMPLING_PASSTHROUGH = (
    "temperature", "top_p", "max_tokens", "stop",
    "presence_penalty", "frequency_penalty", "seed", "n",
)
# Alias tipici di Ollama -> nome OpenAI.
_SAMPLING_ALIASES = {
    "num_predict": "max_tokens",
}


def build_sampling_params(options):
    """Traduce le opzioni (stile Ollama o OpenAI) nei parametri accettati dall'upstream.

    Copre i due mondi:
      - Ollama:      options={"num_predict": ..., "temperature": ..., "stop": [...]}
      - OpenAI/llama.cpp: chiavi gia' in nome OpenAI (temperature, top_p, max_tokens, stop, ...)
    Le chiavi sconosciute vengono ignorate per non far fallire l'upstream con un 400.
    """
    options = options or {}
    params = {}
    for key in _SAMPLING_PASSTHROUGH:
        if options.get(key) is not None:
            params[key] = options[key]
    for src, dst in _SAMPLING_ALIASES.items():
        if options.get(src) is not None and dst not in params:
            params[dst] = options[src]
    return params


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


def _retry_delay(attempt, retry_after=None):
    """Ritardo prima del prossimo tentativo: rispetta Retry-After se presente, altrimenti backoff esponenziale."""
    if retry_after:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return RETRY_BACKOFF * (2 ** attempt)


def _post_upstream(payload, stream, path="/chat/completions"):
    """POST verso NVIDIA con retry sugli errori transitori e logging di richiesta/risposta/telemetria."""
    rid = _request_id()
    model = payload.get("model")
    messages = payload.get("messages") or []
    if messages:
        input_chars = sum(len(str(m.get("content", ""))) for m in messages)
    else:
        raw_input = payload.get("input") or payload.get("prompt") or ""
        input_chars = len(raw_input) if isinstance(raw_input, str) else sum(len(str(x)) for x in raw_input)

    logger.info(
        "[%s] -> NVIDIA request | path=%s model=%s stream=%s messages=%d input_chars=%d",
        rid, path, model, stream, len(messages), input_chars,
    )
    logger.debug("[%s] -> NVIDIA payload: %s", rid, json.dumps(payload)[:2000])

    url = f"{NVIDIA_API_BASE}{path}"
    attempt = 0
    while True:
        start = time.perf_counter()
        try:
            resp = requests.post(
                url,
                headers=nvidia_headers(),
                json=payload,
                stream=stream,
                timeout=UPSTREAM_TIMEOUT,
            )
        except requests.exceptions.RequestException as err:
            elapsed_ms = (time.perf_counter() - start) * 1000
            if attempt < RETRY_MAX:
                delay = _retry_delay(attempt)
                logger.warning(
                    "[%s] <- NVIDIA no-response after %.0fms (tentativo %d/%d), retry tra %.1fs | %s",
                    rid, elapsed_ms, attempt + 1, RETRY_MAX + 1, delay, err,
                )
                time.sleep(delay)
                attempt += 1
                continue
            logger.error("[%s] <- NVIDIA no-response after %.0fms | %s", rid, elapsed_ms, err)
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Errore transitorio: chiudi e ritenta (lo status e' noto prima di leggere il corpo/stream).
        if resp.status_code in RETRY_STATUS and attempt < RETRY_MAX:
            delay = _retry_delay(attempt, resp.headers.get("Retry-After"))
            logger.warning(
                "[%s] <- NVIDIA status=%s dopo %.0fms (tentativo %d/%d), retry tra %.1fs",
                rid, resp.status_code, elapsed_ms, attempt + 1, RETRY_MAX + 1, delay,
            )
            resp.close()
            time.sleep(delay)
            attempt += 1
            continue

        break

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
        # Telemetria dei token (disponibile solo per le risposte non in streaming qui).
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
    payload.update(build_sampling_params(options))
    if stream:
        # Chiediamo l'usage anche in streaming, cosi' possiamo loggarlo e riesporlo.
        payload["stream_options"] = {"include_usage": True}

    return _post_upstream(payload, stream)


def call_nvidia_passthrough(payload, stream, model=None):
    """Inoltra un payload gia' in formato OpenAI (usato da /v1/*), forzando solo model/stream."""
    payload = dict(payload)
    payload["model"] = model or DEFAULT_MODEL
    payload["stream"] = stream

    return _post_upstream(payload, stream)


def call_nvidia_embeddings(payload):
    """POST verso l'endpoint /embeddings dell'upstream."""
    return _post_upstream(payload, stream=False, path="/embeddings")


def iter_nvidia_sse(resp, usage_out=None):
    """Estrae il testo dei delta da uno stream SSE in formato OpenAI.

    Se usage_out (dict) e' fornito, vi accumula l'oggetto `usage` finale
    emesso dall'upstream quando e' attivo stream_options.include_usage.
    """
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
        if usage_out is not None and chunk.get("usage"):
            usage_out.update(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


def _log_stream_usage(rid, usage):
    if usage:
        logger.info(
            "[%s] telemetry (stream) | prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )


def _unauthorized():
    return jsonify({"error": {"message": "unauthorized", "type": "authentication_error"}}), 401


def _check_auth():
    """Se PROXY_API_KEY e' impostata, valida il token della richiesta. Ritorna una risposta 401 se invalido."""
    if not PROXY_API_KEY or request.path in AUTH_EXEMPT_PATHS:
        return None
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    token = token or request.headers.get("X-Api-Key", "").strip()
    if token != PROXY_API_KEY:
        return _unauthorized()
    return None


@app.before_request
def _log_request_start():
    """Assegna un ID di correlazione, applica l'autenticazione e logga la richiesta in ingresso."""
    g.req_id = uuid.uuid4().hex[:8]
    g.req_start = time.perf_counter()

    unauthorized = _check_auth()
    if unauthorized is not None:
        logger.warning(
            "[%s] --> %s %s | client=%s AUTH FAILED",
            g.req_id, request.method, request.path, request.remote_addr,
        )
        return unauthorized

    body = request.get_json(silent=True) or {}
    logger.info(
        "[%s] --> %s %s | client=%s model=%s stream=%s",
        g.req_id, request.method, request.path,
        request.remote_addr,
        body.get("model") if isinstance(body, dict) else None,
        body.get("stream") if isinstance(body, dict) else None,
    )
    return None


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

    rid = getattr(g, "req_id", None)

    def generate():
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield json.dumps({
                "model": model,
                "created_at": now_iso(),
                "message": {"role": "assistant", "content": piece},
                "done": False,
            }) + "\n"
        _log_stream_usage(rid, usage)
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

    rid = getattr(g, "req_id", None)

    def generate():
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield json.dumps({
                "model": model,
                "created_at": now_iso(),
                "response": piece,
                "done": False,
            }) + "\n"
        _log_stream_usage(rid, usage)
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


@app.route("/api/embeddings", methods=["POST"])
def api_embeddings():
    """Embeddings in stile Ollama (legacy): {"model", "prompt"} -> {"embedding": [...]}"""
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    text = body.get("prompt", "")
    model = resolve_embeddings_model(body.get("model"))

    payload = {"model": model, "input": text}
    if EMBEDDINGS_INPUT_TYPE:
        payload["input_type"] = EMBEDDINGS_INPUT_TYPE

    data = call_nvidia_embeddings(payload).json()
    embedding = (data.get("data") or [{}])[0].get("embedding", [])
    return jsonify({"embedding": embedding})


@app.route("/api/embed", methods=["POST"])
def api_embed():
    """Embeddings in stile Ollama (nuovo): {"model", "input"} -> {"embeddings": [[...]]}"""
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    inp = body.get("input", "")
    model = resolve_embeddings_model(body.get("model"))

    payload = {"model": model, "input": inp}
    if EMBEDDINGS_INPUT_TYPE:
        payload["input_type"] = EMBEDDINGS_INPUT_TYPE

    data = call_nvidia_embeddings(payload).json()
    embeddings = [d.get("embedding", []) for d in (data.get("data") or [])]
    return jsonify({"model": model, "embeddings": embeddings})


@app.route("/health", methods=["GET"])
def health():
    """Liveness di base; con ?upstream=1 verifica anche la raggiungibilita' di NVIDIA."""
    info = {
        "status": "ok",
        "api_key_configured": bool(NVIDIA_API_KEY),
        "models": len(NVIDIA_MODELS),
        "default_model": DEFAULT_MODEL,
    }

    if request.args.get("upstream", "").lower() in ("1", "true", "yes"):
        try:
            r = requests.get(f"{NVIDIA_API_BASE}/models", headers=nvidia_headers(), timeout=5)
            info["upstream"] = "ok" if r.ok else f"error:{r.status_code}"
            if not r.ok:
                info["status"] = "degraded"
        except requests.exceptions.RequestException:
            info["upstream"] = "unreachable"
            info["status"] = "degraded"
        return jsonify(info), 200 if info["status"] == "ok" else 503

    return jsonify(info)


@app.route("/props", methods=["GET"])
def props():
    return jsonify({
        "default_generation_settings": {"model": DEFAULT_MODEL, "n_ctx": 4096},
        "total_slots": 1,
        "model_path": DEFAULT_MODEL,
        "chat_template": "",
    })


def _model_entry(name, created):
    return {"id": name, "object": "model", "created": created, "owned_by": "nvidia"}


@app.route("/v1/models", methods=["GET"])
def v1_models():
    created = int(time.time())
    return jsonify({
        "object": "list",
        "data": [_model_entry(name, created) for name in NVIDIA_MODELS],
    })


@app.route("/v1/models/<path:model_id>", methods=["GET"])
def v1_model(model_id):
    """Dettaglio di un singolo modello (alcuni SDK OpenAI lo interrogano)."""
    if model_id not in NVIDIA_MODELS:
        return jsonify({"error": {"message": f"model '{model_id}' not found", "type": "not_found"}}), 404
    return jsonify(_model_entry(model_id, int(time.time())))


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


@app.route("/v1/embeddings", methods=["POST"])
def v1_embeddings():
    """Embeddings in formato OpenAI (passthrough verso /embeddings dell'upstream)."""
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    payload = dict(body)
    payload["model"] = resolve_embeddings_model(body.get("model"))
    if EMBEDDINGS_INPUT_TYPE and "input_type" not in payload:
        payload["input_type"] = EMBEDDINGS_INPUT_TYPE

    data = call_nvidia_embeddings(payload).json()
    data["model"] = payload["model"]
    return jsonify(data)


@app.route("/v1/completions", methods=["POST"])
def v1_completions():
    if not NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY non configurata nel file .env"}), 500

    body = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "".join(prompt)
    stream = body.get("stream", False)
    options = build_sampling_params(body)
    model = resolve_model(body.get("model"))

    messages = [{"role": "user", "content": prompt}]
    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
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

    rid = getattr(g, "req_id", None)

    def generate():
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield "data: " + json.dumps({
                "id": "cmpl-llmproxy",
                "object": "text_completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"text": piece, "index": 0, "logprobs": None, "finish_reason": None}],
            }) + "\n\n"
        _log_stream_usage(rid, usage)
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
    # llama.cpp usa n_predict/n_probs ecc.; normalizziamo gli alias piu' comuni.
    options = build_sampling_params(body)
    if body.get("n_predict") is not None and "max_tokens" not in options:
        options["max_tokens"] = body["n_predict"]
    model = resolve_model(body.get("model"))

    messages = [{"role": "user", "content": prompt}]
    upstream = call_nvidia(messages, stream, options, model=model)

    if not stream:
        data = upstream.json()
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

    rid = getattr(g, "req_id", None)

    def generate():
        usage = {}
        for piece in iter_nvidia_sse(upstream, usage):
            yield "data: " + json.dumps({
                "content": piece,
                "model": model,
                "stop": False,
            }) + "\n\n"
        _log_stream_usage(rid, usage)
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
    logger.info("Autenticazione in ingresso: %s", "ATTIVA" if PROXY_API_KEY else "disattivata")
    app.run(host=HOST, port=PORT, threaded=True)
