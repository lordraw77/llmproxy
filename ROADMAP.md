# Roadmap

Development direction for **llmproxy**. Dates are indicative; priority is driven
by items marked as "core". Versioning follows [SemVer](https://semver.org/).

Current state — **v1.3.0**: a **multi-provider** proxy that exposes the Ollama,
OpenAI and llama.cpp APIs over several upstreams at once (OpenAI-compatible,
Azure, Anthropic and Gemini), with model→provider routing, embeddings,
retry/backoff, inbound auth, an optional response cache and in-process metrics.
Since v1.1.0 the line has added, without breaking changes:

- **v1.1.1** — `FORCE_UPSTREAM_STREAM`: always stream towards the upstream and
  transparently re-aggregate into a non-streaming reply, avoiding read timeouts
  on slow/non-streaming generations.
- **v1.1.2** — outbound **HTTP/HTTPS proxy** support
  (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`) for corporate egress networks.
- **v1.1.3** — prefix-less OpenAI routes (served with and without `/v1`) and
  `DEBUG` logging of the upstream response body.
- **v1.1.4** — fix: preserve **tool calls** (and the real `finish_reason`) when
  re-aggregating a `FORCE_UPSTREAM_STREAM` response, so forced/auto function
  calls are no longer lost on non-streaming requests.
- **v1.2.0** — **response caching** (`CACHE_ENABLED`/`CACHE_TTL`/`CACHE_MAX_SIZE`):
  identical non-streaming completions and embeddings are served from a per-worker
  in-memory TTL + LRU cache, with hit/miss/eviction stats exposed at `/stats`.
- **v1.3.0** — **multi-provider**: a `Provider` abstraction with
  OpenAI-compatible/Azure/Anthropic/Gemini implementations, declarative
  `providers.toml` (env fallback + `make migrate-config`), and model→provider
  routing exposing every provider's models (`provider:model`, optional aliases).

---

## v1.3.0 — Multi-provider (core) — shipped

Goal: remove the coupling to the single NVIDIA upstream and introduce a provider
abstraction.

- [x] Abstract `Provider` interface; the NVIDIA upstream becomes one
      `OpenAICompatibleProvider` implementation.
- [x] Additional providers: **OpenAI**, **Anthropic**, **Azure OpenAI**,
      **Google (Gemini)**, **Mistral**, **local Ollama/llama.cpp**, generic
      **OpenAI-compatible** endpoints (vLLM, LM Studio, Groq, OpenRouter…).
- [x] Declarative multi-provider configuration (`providers.toml`, with env-var
      interpolation) plus a backward-compatible `NVIDIA_*` env fallback and a
      `make migrate-config` tool.
- [x] **Model → provider** map: all providers' models are exposed together (bare
      names with a single provider, prefixed `provider:model` with 2+, plus
      optional per-model aliases), each routed to its owning provider.
- [x] Normalize API differences (parameter names, embeddings formats, streaming
      formats) behind the abstraction (Anthropic + Gemini native translation).

## v1.4.0 — Model and provider fail-chain (core)

Goal: service continuity when a model or a provider is unavailable.

- [ ] **Model fail-chain**: ordered fallback list for a logical model
      (e.g. `llama-3.1-70b → llama-3.1-8b`).
- [ ] **Provider fail-chain**: the **same model** served by multiple providers,
      with automatic failover on error/timeout/rate-limit.
- [ ] Selection strategies: **fixed priority**, **round-robin**, **least-latency**,
      **free/random** (load balancing).
- [ ] **Circuit breaker** per provider, with backoff and automatic re-enablement.
- [ ] Active **health checks** of upstreams and temporary removal of degraded ones
      from rotation.
- [ ] Failover-aware retry policies (avoid double-retrying the same failed node).

## v1.5.0 — Networking and connectivity

Goal: run inside corporate networks and isolated environments.

- [x] **Outbound HTTP/HTTPS proxy** for upstream connections, with `NO_PROXY`
      *(shipped in v1.1.2)*.
- [ ] Extend it: **SOCKS5** support and **per-provider** proxy settings.
- [ ] **TLS** configuration: custom CA, mTLS to upstreams, configurable
      certificate verification.
- [ ] Granular timeouts and keep-alive per provider; connection-pool tuning.
- [ ] **IPv6** support and binding on multiple interfaces.

---

## Additional proposals (to be evaluated)

Ideas worth considering, ordered by value/effort ratio.

### Reliability & performance
- [x] **Response caching** (by prompt+parameters) with TTL, for embeddings and
      deterministic completions — big savings on repetitive workloads.
      *(shipped in v1.2.0: `CACHE_ENABLED`/`CACHE_TTL`/`CACHE_MAX_SIZE`, per-worker
      TTL + LRU, non-streaming only, stats at `/stats`.)*
- [ ] **Rate limiting / quotas** per inbound API key (tokens/min, requests/min).
- [ ] **Queue and max concurrency** towards upstreams to avoid cascading 429s.
- [ ] **Robust streaming**: upstream cancellation when the client disconnects.
      *(Partially addressed in v1.1.1: `FORCE_UPSTREAM_STREAM` streams upstream
      and re-aggregates to dodge non-streaming read timeouts.)*

### Observability
- [ ] **Prometheus `/metrics`** endpoint (in-process metrics already exist).
- [ ] **OpenTelemetry tracing** (per-request spans, correlation-id propagation).
- [ ] **Estimated cost/token** logging per provider and per API key.
- [ ] Minimal **dashboard** (Grafana or HTML page) for throughput, latency, errors.

### Security & multi-tenant
- [ ] **Multiple inbound API keys** with per-key permissions/quotas/model allowlist.
- [ ] **Secret redaction** in logs and optional masking of sensitive prompts.
- [ ] Upstream key management via a **secret manager** (Vault, env, file).

### API features
- [ ] **Semantic router / routing policies** (e.g. route by prompt length,
      language, cost, or request tag).
- [ ] Normalized **tool/function calling** and **JSON mode** across providers.
- [ ] **Multimodal** support (images/audio) where providers allow it.
- [ ] Configurable **prompt transformations** (system-prompt injection, templates).
- [ ] **`/v1/models`** endpoint populated dynamically from configured providers.

### Operations
- [ ] **Hot reload** of configuration without a restart.
- [ ] **Helm chart / Kubernetes manifests** with dedicated liveness/readiness probes.
- [ ] **Integration tests** with a mock provider and CI (GitHub Actions).
- [ ] **Multi-arch** Docker images (amd64/arm64).

---

### Legend
- **core**: explicit, high-priority goals.
- Items under "Additional proposals" will be promoted to a versioned milestone as
  they are selected.
