"""Low-level HTTP client for the NVIDIA OpenAI-compatible upstream.

Owns the connection pool, the retry/backoff policy, the upstream authentication
headers, and the request/response/telemetry logging. This is the only module in
the application that talks to the network towards the provider.
"""

import json
import logging
import time

import requests
from requests.adapters import HTTPAdapter


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

    @property
    def headers(self):
        """The (cached) HTTP headers used for every upstream NVIDIA request."""
        return self._headers

    def get(self, path, timeout):
        """Bare GET against the upstream (used by the health check)."""
        return requests.get(f"{self._settings.nvidia_api_base}{path}", headers=self._headers, timeout=timeout)

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

        # input_chars scans the whole input: computed only if it will actually be logged.
        if logger.isEnabledFor(logging.INFO):
            if messages:
                input_chars = sum(len(str(m.get("content", ""))) for m in messages)
            else:
                raw_input = payload.get("input") or payload.get("prompt") or ""
                input_chars = len(raw_input) if isinstance(raw_input, str) else sum(len(str(x)) for x in raw_input)
            logger.info(
                "[%s] -> NVIDIA request | path=%s model=%s stream=%s messages=%d input_chars=%d",
                rid, path, model, stream, len(messages), input_chars,
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
                    stream=stream,
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
            rid, resp.status_code, elapsed_ms, stream,
        )

        if not resp.ok:
            # The error body (for a non-2xx) is small: log it to understand why.
            logger.warning("[%s] <- NVIDIA error body: %s", rid, resp.text[:1000])
        elif not stream:
            # Token telemetry (available only for non-streaming responses here).
            try:
                usage = resp_json(resp).get("usage") or {}
            except ValueError:
                usage = {}
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
