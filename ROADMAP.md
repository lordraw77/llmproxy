# Roadmap

Development direction for **llmproxy**. Dates are indicative; priority is driven
by items marked as "core". Versioning follows [SemVer](https://semver.org/).

Current state — **v1.1.4**: a proxy to a **single provider** (NVIDIA,
OpenAI-compatible API) that exposes the Ollama, OpenAI and llama.cpp APIs, with
embeddings, retry/backoff, inbound auth and in-process metrics. Since v1.1.0 the
`1.1.x` line has added, without breaking changes:

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

---

## v1.2.0 — Multi-provider (core)

Goal: remove the coupling to the single NVIDIA upstream and introduce a provider
abstraction.

- [ ] Abstract `Provider` interface; `NvidiaUpstream` becomes one implementation.
- [ ] Additional providers: **OpenAI**, **Anthropic**, **Azure OpenAI**,
      **Google (Gemini)**, **Mistral**, **local Ollama/llama.cpp**, generic
      **OpenAI-compatible** endpoints (vLLM, LM Studio, Groq, OpenRouter…).
- [ ] Declarative multi-provider configuration (YAML/TOML in addition to env vars),
      with per-provider credentials and base URLs.
- [ ] **Model → provider** map: the same exposed model name can be served by
      different providers.
- [ ] Normalize API differences (parameter names, embeddings formats, streaming
      formats) behind the abstraction.

## v1.3.0 — Model and provider fail-chain (core)

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

## v1.4.0 — Networking and connectivity

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
- [ ] **Response caching** (by prompt+parameters) with TTL, for embeddings and
      deterministic completions — big savings on repetitive workloads.
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
