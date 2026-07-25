"""Provider abstraction and the shared upstream HTTP machinery.

A :class:`Provider` owns the connection pool, the retry/backoff policy, the
per-request telemetry logging, and the ``FORCE_UPSTREAM_STREAM`` re-aggregation.
Everything that differs between upstreams — the URL, the authentication headers,
and the request/response/stream *shape* — is delegated to a handful of hooks that
subclasses override. The default hook implementations speak the OpenAI dialect,
so an OpenAI-compatible endpoint needs no overrides at all.

The contract every provider exposes to the rest of the app is uniform and
OpenAI-shaped: :func:`resp_json` on the returned object yields an OpenAI
``chat.completion``/``embeddings`` dict, and a streaming response yields
OpenAI-format SSE consumed by :func:`llmproxy.upstream.sse.iter_nvidia_sse`.
Native providers (Anthropic, Gemini) translate their own formats into this shape
internally, which keeps the whole ``web/routes`` layer provider-agnostic.
"""

import json
import logging
import time

import requests
from requests.adapters import HTTPAdapter

from ..upstream.sse import iter_nvidia_sse


class AggregatedResponse:
    """A synthetic non-streaming response built from an already-parsed JSON body.

    Exposes the subset of the ``requests.Response`` interface the web layer uses
    (``ok``, ``status_code``, ``raise_for_status``, and the memoized
    :func:`resp_json`). Used both for the ``FORCE_UPSTREAM_STREAM`` re-aggregation
    and by native providers that translate their response into an OpenAI dict.
    """

    def __init__(self, data):
        self._llmproxy_json = data
        self.status_code = 200
        self.ok = True
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._llmproxy_json


def resp_json(resp):
    """Parse ``resp`` as JSON once and memoize the result on the response object.

    ``requests`` does not memoize ``.json()``: it re-parses the body on every call.
    Caching it here lets telemetry and the endpoint handler share a single parse
    of the response payload.
    """
    cached = getattr(resp, "_llmproxy_json", None)
    if cached is None:
        cached = resp.json()
        resp._llmproxy_json = cached
    return cached


class TranslatedStream:
    """Wraps a native streaming response, re-emitting it as OpenAI-format SSE.

    Native providers hand this a generator of OpenAI ``chat.completion.chunk``
    dicts (built from their own event stream); it exposes the two read surfaces
    the web layer relies on: ``iter_lines`` (consumed by
    :func:`~llmproxy.upstream.sse.iter_nvidia_sse` for re-framing/aggregation) and
    ``iter_content`` (the raw byte relay in the OpenAI ``/v1/chat/completions``
    route). OpenAI-compatible providers skip this entirely and return the raw
    ``requests.Response``.
    """

    def __init__(self, chunks, raw=None):
        self._chunks = chunks
        self._raw = raw
        self.status_code = 200
        self.ok = True

    def iter_lines(self, decode_unicode=True):
        for chunk in self._chunks:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            yield ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"

    def close(self):
        if self._raw is not None:
            self._raw.close()


class Provider:
    """Connection-pooled client for one upstream, with retries and telemetry.

    Subclasses override the hooks (:meth:`_url`, :meth:`_headers`,
    :meth:`_build_body`, :meth:`_normalize_nonstream`, :meth:`_normalize_stream`)
    to adapt a non-OpenAI upstream; the default hooks speak OpenAI directly.
    """

    def __init__(self, config, settings, logger, metrics=None):
        self._config = config
        self._settings = settings
        self._logger = logger
        self._metrics = metrics
        self._headers = self._build_headers()
        # One keep-alive Session per worker (created post-fork so no connection is
        # inherited across a gunicorn fork). Retries are handled manually below.
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=settings.pool_size,
            pool_maxsize=settings.pool_size,
            max_retries=0,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        proxies = config.proxy or settings.proxies
        if proxies:
            self._session.proxies.update(proxies)

    # --- identity ---------------------------------------------------------

    @property
    def name(self):
        """The configured provider name (used as the model-name prefix)."""
        return self._config.name

    @property
    def headers(self):
        """The (cached) HTTP headers used for every request to this provider."""
        return self._headers

    @property
    def timeout(self):
        return self._config.timeout or self._settings.upstream_timeout

    # --- hooks (OpenAI defaults; overridden by native providers) ----------

    def _build_headers(self):
        """Return the constant auth/content headers for this provider."""
        headers = {
            self._config.auth_header: self._config.auth_value,
            "Content-Type": "application/json",
        }
        headers.update(self._config.extra_headers or {})
        return headers

    def _url(self, path, stream, model=None):
        """Absolute URL for ``path``.

        ``stream`` lets native providers switch verbs (e.g. Gemini's
        ``streamGenerateContent``); ``model`` lets Azure put the deployment in the
        path. The OpenAI default ignores both.
        """
        return f"{self._config.base_url}{path}"

    def _build_body(self, payload, path, stream, aggregate):
        """Build the JSON body sent upstream from the canonical OpenAI ``payload``.

        Default (OpenAI dialect): forward as-is, set ``stream``, and ask for usage
        when we will need to re-aggregate the stream into a non-streaming reply.
        """
        body = dict(payload)
        body["stream"] = stream
        if aggregate:
            body["stream_options"] = {"include_usage": True}
        return body

    def _normalize_nonstream(self, resp):
        """Return an OpenAI-shaped non-streaming response.

        Default: the raw ``requests.Response`` is already OpenAI-shaped.
        """
        return resp

    def _normalize_stream(self, resp):
        """Return a stream whose ``iter_lines``/``iter_content`` emit OpenAI SSE.

        Default: the raw ``requests.Response`` already yields OpenAI SSE.
        """
        return resp

    # --- shared behaviour -------------------------------------------------

    def log_cache_hit(self, rid, key):
        """Log a response-cache hit (the upstream call is skipped)."""
        self._logger.info(
            "[%s] cache HIT | provider=%s key=%s (upstream call skipped)",
            rid, self.name, key[-12:],
        )

    #: Lightweight GET endpoint used by the health check (relative to base_url).
    models_path = "/models"

    def health(self, timeout):
        """GET a lightweight endpoint to probe reachability (used by ``/health``)."""
        return self._session.get(
            f"{self._config.base_url}{self.models_path}", headers=self._headers, timeout=timeout,
        )

    def _retry_delay(self, attempt, retry_after=None):
        """Delay before the next attempt: honor ``Retry-After``, else exponential backoff."""
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return self._settings.retry_backoff * (2 ** attempt)

    def post(self, payload, stream, rid, path="/chat/completions"):
        """POST ``payload`` upstream with retries, telemetry, and normalization.

        Returns an OpenAI-shaped response: a non-streaming reply on which
        :func:`resp_json` yields an OpenAI dict, or a stream yielding OpenAI SSE.
        """
        settings = self._settings
        logger = self._logger
        model = payload.get("model")
        messages = payload.get("messages") or []

        # Force streaming upstream (only for chat completions). Re-aggregate into a
        # single reply when the caller wanted non-streaming.
        force = settings.force_upstream_stream and path == "/chat/completions"
        aggregate = force and not stream
        upstream_stream = stream or force

        body = self._build_body(payload, path, upstream_stream, aggregate)
        url = self._url(path, upstream_stream, model)

        if logger.isEnabledFor(logging.INFO):
            if messages:
                input_chars = sum(len(str(m.get("content", ""))) for m in messages)
            else:
                raw_input = payload.get("input") or payload.get("prompt") or ""
                input_chars = len(raw_input) if isinstance(raw_input, str) else sum(len(str(x)) for x in raw_input)
            logger.info(
                "[%s] -> %s request | path=%s model=%s stream=%s%s messages=%d input_chars=%d",
                rid, self.name, path, model, upstream_stream,
                " (forced, aggregated)" if aggregate else "",
                len(messages), input_chars,
            )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[%s] -> %s payload: %s", rid, self.name, json.dumps(body)[:2000])

        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                resp = self._session.post(
                    url,
                    headers=self._headers,
                    json=body,
                    stream=upstream_stream,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as err:
                elapsed_ms = (time.perf_counter() - start) * 1000
                if attempt < settings.retry_max:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "[%s] <- %s no-response after %.0fms (tentativo %d/%d), retry tra %.1fs | %s",
                        rid, self.name, elapsed_ms, attempt + 1, settings.retry_max + 1, delay, err,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                logger.error("[%s] <- %s no-response after %.0fms | %s", rid, self.name, elapsed_ms, err)
                if self._metrics is not None:
                    self._metrics.record_upstream(elapsed_ms, ok=False)
                raise

            elapsed_ms = (time.perf_counter() - start) * 1000

            if resp.status_code in settings.retry_status and attempt < settings.retry_max:
                delay = self._retry_delay(attempt, resp.headers.get("Retry-After"))
                logger.warning(
                    "[%s] <- %s status=%s dopo %.0fms (tentativo %d/%d), retry tra %.1fs",
                    rid, self.name, resp.status_code, elapsed_ms, attempt + 1, settings.retry_max + 1, delay,
                )
                resp.close()
                time.sleep(delay)
                attempt += 1
                continue

            break

        if self._metrics is not None:
            self._metrics.record_upstream(elapsed_ms, ok=resp.ok)

        level = logging.INFO if resp.ok else logging.WARNING
        logger.log(
            level,
            "[%s] <- %s response | status=%s latency=%.0fms stream=%s",
            rid, self.name, resp.status_code, elapsed_ms, upstream_stream,
        )

        if not resp.ok:
            logger.warning("[%s] <- %s error body: %s", rid, self.name, resp.text[:1000])
            resp.raise_for_status()

        # Streaming upstream: normalize to OpenAI SSE, aggregating if requested.
        if upstream_stream:
            wrapped = self._normalize_stream(resp)
            if aggregate:
                return self._aggregate_stream(wrapped, model, rid, elapsed_ms)
            return wrapped

        normalized = self._normalize_nonstream(resp)
        self._log_tokens(normalized, rid, elapsed_ms)
        return normalized

    def _log_tokens(self, resp, rid, elapsed_ms):
        """Emit token telemetry from a normalized non-streaming response."""
        logger = self._logger
        try:
            body = resp_json(resp)
        except ValueError:
            return
        if body and logger.isEnabledFor(logging.DEBUG):
            logger.debug("[%s] <- %s response body: %s", rid, self.name, json.dumps(body)[:2000])
        usage = body.get("usage") or {}
        if usage:
            if self._metrics is not None:
                self._metrics.record_tokens(usage)
            logger.info(
                "[%s] telemetry | prompt_tokens=%s completion_tokens=%s total_tokens=%s latency=%.0fms",
                rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("total_tokens"), elapsed_ms,
            )

    def _aggregate_stream(self, resp, model, rid, elapsed_ms):
        """Consume an OpenAI-SSE stream and rebuild a non-streaming chat.completion."""
        usage = {}
        meta = {}
        parts = [piece for piece in iter_nvidia_sse(resp, usage, meta)]
        content = "".join(parts)
        tool_calls = meta.get("tool_calls")
        finish_reason = meta.get("finish_reason") or ("tool_calls" if tool_calls else "stop")

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "[%s] <- %s response body (aggregated): content=%s tool_calls=%s",
                rid, self.name, json.dumps(content)[:2000], json.dumps(tool_calls)[:2000],
            )

        if not usage:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self._metrics is not None:
            self._metrics.record_tokens(usage)
        self._logger.info(
            "[%s] telemetry (aggregated) | prompt_tokens=%s completion_tokens=%s total_tokens=%s latency=%.0fms",
            rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            usage.get("total_tokens"), elapsed_ms,
        )

        message = {"role": "assistant", "content": content or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        data = {
            "id": "chatcmpl-llmproxy",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason,
            }],
            "usage": usage,
        }
        return AggregatedResponse(data)
