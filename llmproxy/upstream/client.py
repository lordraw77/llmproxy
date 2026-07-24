"""Low-level HTTP client for the NVIDIA OpenAI-compatible upstream.

Owns the connection pool, the retry/backoff policy, the upstream authentication
headers, and the request/response/telemetry logging. This is the only module in
the application that talks to the network towards the provider.
"""

import json
import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter

from .sse import iter_nvidia_sse


class AggregatedResponse:
    """A synthetic non-streaming response built from a consumed SSE stream.

    When the proxy is configured to always stream towards the upstream but the
    caller asked for a non-streaming reply, the SSE stream is re-assembled into a
    single OpenAI ``chat.completion`` object. This object exposes the subset of
    the ``requests.Response`` interface the web layer uses (``ok``,
    ``status_code``, ``raise_for_status``, and the memoized ``resp_json``).
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


class NvidiaUpstream:
    """Connection-pooled client that POSTs to the NVIDIA upstream with retries and logging."""

    def __init__(self, settings, logger, metrics=None):
        self._settings = settings
        self._logger = logger
        self._metrics = metrics
        # Constant for the process: build the headers once.
        self._headers = {
            "Authorization": f"Bearer {settings.nvidia_api_key}",
            "Content-Type": "application/json",
        }
        # A single Session (per worker) with keep-alive: it reuses TCP/TLS
        # connections instead of repeating the handshake on every request. The
        # pool is sized on the number of worker threads plus some margin. Created
        # here (per worker, post-fork) so no open connection is inherited across a
        # gunicorn fork.
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=settings.pool_size,
            pool_maxsize=settings.pool_size,
            max_retries=0,  # retries handled manually below (with backoff/Retry-After).
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        # Outbound egress proxy. When set explicitly it is applied to the
        # session; NO_PROXY is exported to the environment so requests' own
        # bypass logic (trust_env stays on) keeps honoring it for both the
        # session and the health-check GET below.
        if settings.proxies:
            self._session.proxies.update(settings.proxies)
        if settings.no_proxy and "NO_PROXY" not in os.environ:
            os.environ["NO_PROXY"] = settings.no_proxy

    @property
    def headers(self):
        """The (cached) HTTP headers used for every upstream NVIDIA request."""
        return self._headers

    def get(self, path, timeout):
        """Bare GET against the upstream (used by the health check)."""
        return requests.get(
            f"{self._settings.nvidia_api_base}{path}",
            headers=self._headers,
            timeout=timeout,
            proxies=self._settings.proxies,
        )

    def _retry_delay(self, attempt, retry_after=None):
        """Compute the delay before the next attempt.

        Honors the ``Retry-After`` value when present, otherwise uses exponential backoff.
        """
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return self._settings.retry_backoff * (2 ** attempt)

    def post(self, payload, stream, rid, path="/chat/completions"):
        """POST to NVIDIA with retries on transient errors plus request/response/telemetry logging.

        Args:
            payload: The JSON body to send to the upstream.
            stream: Whether to open the response as a stream (SSE).
            rid: Correlation id for the log lines.
            path: Upstream path appended to the API base (default: chat completions).

        Returns:
            The successful ``requests.Response`` (already ``raise_for_status``-checked).

        Raises:
            requests.exceptions.RequestException: On network failure after exhausting
                retries, or on a non-2xx status via ``raise_for_status``.
        """
        settings = self._settings
        logger = self._logger
        model = payload.get("model")
        messages = payload.get("messages") or []

        # Force streaming towards the upstream (only for chat completions, the
        # only path that returns an SSE stream). When the caller wanted a
        # non-streaming reply we transparently re-aggregate the stream below.
        force = settings.force_upstream_stream and path == "/chat/completions"
        aggregate = force and not stream
        upstream_stream = stream or force
        if aggregate:
            payload = dict(payload)
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        # input_chars scans the whole input: computed only if it will actually be logged.
        if logger.isEnabledFor(logging.INFO):
            if messages:
                input_chars = sum(len(str(m.get("content", ""))) for m in messages)
            else:
                raw_input = payload.get("input") or payload.get("prompt") or ""
                input_chars = len(raw_input) if isinstance(raw_input, str) else sum(len(str(x)) for x in raw_input)
            logger.info(
                "[%s] -> NVIDIA request | path=%s model=%s stream=%s%s messages=%d input_chars=%d",
                rid, path, model, upstream_stream,
                " (forced, aggregated)" if aggregate else "",
                len(messages), input_chars,
            )
        # json.dumps of the whole payload: debug only (otherwise we waste CPU/memory on every request).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[%s] -> NVIDIA payload: %s", rid, json.dumps(payload)[:2000])

        url = f"{settings.nvidia_api_base}{path}"
        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                resp = self._session.post(
                    url,
                    headers=self._headers,
                    json=payload,
                    stream=upstream_stream,
                    timeout=settings.upstream_timeout,
                )
            except requests.exceptions.RequestException as err:
                elapsed_ms = (time.perf_counter() - start) * 1000
                if attempt < settings.retry_max:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "[%s] <- NVIDIA no-response after %.0fms (tentativo %d/%d), retry tra %.1fs | %s",
                        rid, elapsed_ms, attempt + 1, settings.retry_max + 1, delay, err,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                logger.error("[%s] <- NVIDIA no-response after %.0fms | %s", rid, elapsed_ms, err)
                if self._metrics is not None:
                    self._metrics.record_upstream(elapsed_ms, ok=False)
                raise

            elapsed_ms = (time.perf_counter() - start) * 1000

            # Transient error: close and retry (status known before reading body/stream).
            if resp.status_code in settings.retry_status and attempt < settings.retry_max:
                delay = self._retry_delay(attempt, resp.headers.get("Retry-After"))
                logger.warning(
                    "[%s] <- NVIDIA status=%s dopo %.0fms (tentativo %d/%d), retry tra %.1fs",
                    rid, resp.status_code, elapsed_ms, attempt + 1, settings.retry_max + 1, delay,
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
            "[%s] <- NVIDIA response | status=%s latency=%.0fms stream=%s",
            rid, resp.status_code, elapsed_ms, upstream_stream,
        )

        # Caller wanted a non-streaming reply but we streamed the upstream:
        # collapse the SSE stream into a single chat.completion object.
        if resp.ok and aggregate:
            return self._aggregate_stream(resp, model, rid, elapsed_ms)

        if not resp.ok:
            # The error body (for a non-2xx) is small: log it to understand why.
            logger.warning("[%s] <- NVIDIA error body: %s", rid, resp.text[:1000])
        elif not stream:
            # Token telemetry (available only for non-streaming responses here).
            try:
                body = resp_json(resp)
            except ValueError:
                body = {}
            # Full response body: debug only (mirrors the request payload log).
            if body and logger.isEnabledFor(logging.DEBUG):
                logger.debug("[%s] <- NVIDIA response body: %s", rid, json.dumps(body)[:2000])
            usage = body.get("usage") or {}
            if usage:
                if self._metrics is not None:
                    self._metrics.record_tokens(usage)
                logger.info(
                    "[%s] telemetry | prompt_tokens=%s completion_tokens=%s total_tokens=%s latency=%.0fms",
                    rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    usage.get("total_tokens"), elapsed_ms,
                )

        resp.raise_for_status()
        return resp

    def _aggregate_stream(self, resp, model, rid, elapsed_ms):
        """Consume an upstream SSE stream and rebuild a non-streaming chat.completion.

        Used when the caller asked for a non-streaming reply but the proxy was
        configured to always stream towards the upstream. Concatenates the delta
        contents, recovers the final ``usage`` object, and returns an
        :class:`AggregatedResponse` that the web layer treats like any other
        non-streaming upstream response.
        """
        usage = {}
        parts = [piece for piece in iter_nvidia_sse(resp, usage)]
        content = "".join(parts)

        # Reconstructed content: debug only (mirrors the request payload log).
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("[%s] <- NVIDIA response body (aggregated): %s", rid, json.dumps(content)[:2000])

        if not usage:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self._metrics is not None:
            self._metrics.record_tokens(usage)
        self._logger.info(
            "[%s] telemetry (aggregated) | prompt_tokens=%s completion_tokens=%s total_tokens=%s latency=%.0fms",
            rid, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            usage.get("total_tokens"), elapsed_ms,
        )

        data = {
            "id": "chatcmpl-llmproxy",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": None,
                "finish_reason": "stop",
            }],
            "usage": usage,
        }
        return AggregatedResponse(data)
