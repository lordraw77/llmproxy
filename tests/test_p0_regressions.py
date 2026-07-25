"""Regression tests for the three P0 fixes shipped in 1.3.0.

These started life as a manual smoke script run against a live instance; they are
kept here as real tests so the fixes cannot silently regress.

- **F1** — ``/stats`` interpolated the raw ``request.path`` into HTML without
  escaping, and recorded it as a metric key: stored XSS plus unbounded cardinality.
- **F2** — every inference route returned 500 when ``NVIDIA_API_KEY`` was empty,
  which is meaningless under multi-provider configuration.
- **F3** — ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``, so a
  non-ASCII inbound token produced a 500 instead of a 401.
"""

import hmac

import pytest

from llmproxy.providers.base import AggregatedResponse
from llmproxy.providers.openai_compatible import OpenAICompatibleProvider

from .conftest import make_provider_config


XSS = "/<script>alert(1)</script>"


# --- F1: metric cardinality ------------------------------------------------

def test_unmatched_path_collapses_into_a_single_metric_bucket(client):
    """An attacker-chosen path must not become a metric key of its own."""
    client.get(XSS)
    client.get("/another/bogus/path")
    client.get("/yet/another")

    by_path = client.get("/stats.json").get_json()["metrics"]["requests"]["by_path"]
    assert "<unmatched>" in by_path
    assert by_path["<unmatched>"] == 3
    assert XSS not in by_path


def test_matched_routes_are_recorded_under_their_url_rule(client):
    client.get("/v1/models")
    by_path = client.get("/stats.json").get_json()["metrics"]["requests"]["by_path"]
    assert by_path.get("/v1/models") == 1


def test_metric_keys_stay_bounded_under_many_distinct_paths(client):
    """The key space is capped by the routing table, not by client input."""
    for i in range(50):
        client.get(f"/bogus/{i}")
    by_path = client.get("/stats.json").get_json()["metrics"]["requests"]["by_path"]
    assert list(by_path) == ["<unmatched>"]
    assert by_path["<unmatched>"] == 50


def test_stats_endpoints_are_exempt_from_the_counters(client):
    """Observing the metrics must not perturb them."""
    client.get("/stats")
    client.get("/stats.json")
    by_path = client.get("/stats.json").get_json()["metrics"]["requests"]["by_path"]
    assert "/stats" not in by_path and "/stats.json" not in by_path


# --- F1: HTML escaping -----------------------------------------------------

def test_dashboard_escapes_metric_keys(client):
    """Even with a bounded key space, nothing reaches the page unescaped."""
    client.get(XSS)
    html = client.get("/stats").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;unmatched&gt;" in html


def test_dashboard_escapes_configuration_coming_from_providers_toml(app_factory):
    """Model names are operator-controlled, but the renderer must not trust them."""
    hostile = 'x"><script>alert(1)</script>'
    app = app_factory(models=(hostile,), default_model=hostile)
    html = app.test_client().get("/stats").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_still_renders_the_real_values(client):
    """Escaping must not break the page it protects."""
    html = client.get("/stats").get_data(as_text=True)
    assert "<h2>Process manager</h2>" in html
    assert "test-model" in html


# --- F2: no runtime NVIDIA_API_KEY guard -----------------------------------

@pytest.fixture
def offline_upstream(monkeypatch):
    """Neutralize the network: every provider call returns a canned OpenAI reply."""
    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        return AggregatedResponse({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": payload.get("model", ""),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)


HISTORICAL_500 = "NVIDIA_API_KEY non configurata nel file .env"


@pytest.mark.parametrize("path,payload", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/completions", {"prompt": "hi"}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
    ("/completion", {"prompt": "hi"}),
])
def test_inference_routes_do_not_500_without_a_credential(
    app_factory, offline_upstream, path, payload
):
    """A provider needing no key (local Ollama, vLLM) must still be served."""
    client = app_factory(nvidia_api_key="").test_client()
    # ``stream: false`` explicitly: the Ollama dialect streams by default.
    resp = client.post(path, json={"model": "test-model", "stream": False, **payload})
    assert resp.status_code == 200
    assert HISTORICAL_500 not in resp.get_data(as_text=True)


def test_the_historical_guard_helper_is_gone():
    """``require_upstream_key`` must not come back through a stray import."""
    import llmproxy.web.formatting as formatting

    assert not hasattr(formatting, "require_upstream_key")


class RecordingLogger:
    """Collects formatted log lines.

    Used instead of ``caplog`` because the application logger is configured with
    its own handler and does not propagate to the root logger pytest hooks into.
    """

    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)

    debug = error = info


def build_registry(*provider_configs):
    """Run ``build_providers`` over ad-hoc provider configs, capturing its logging."""
    from llmproxy.providers import build_providers

    from .conftest import make_settings

    logger = RecordingLogger()
    registry = build_providers(make_settings(providers=provider_configs), logger)
    return registry, logger


def test_a_provider_without_a_credential_is_reported_at_startup():
    """The check moved from request time to start-up, as a warning — not an error."""
    registry, logger = build_registry(
        make_provider_config("local-ollama", models=("qwen3:8b",), api_key=""),
        make_provider_config("nvidia", models=("meta/llama-3.1-8b-instruct",), api_key="secret"),
    )

    assert any("local-ollama" in m for m in logger.warnings)
    assert not any("nvidia" in m for m in logger.warnings)
    assert len(registry.providers) == 2, "the app is built and served regardless"


def test_an_unresolved_env_reference_counts_as_a_missing_credential():
    """``api_key = "${MISSING}"`` expands to "" at config time and must be caught."""
    _, logger = build_registry(
        make_provider_config("groq", models=("llama-3.3-70b",), api_key=""),
    )
    assert any("groq" in m for m in logger.warnings)


def test_a_whitespace_only_credential_is_treated_as_missing():
    _, logger = build_registry(
        make_provider_config("groq", models=("llama-3.3-70b",), api_key="   "),
    )
    assert any("groq" in m for m in logger.warnings)


def test_no_warning_when_every_provider_is_authenticated():
    _, logger = build_registry(
        make_provider_config("groq", models=("llama-3.3-70b",), api_key="gsk_x"),
        make_provider_config("nvidia", models=("meta/llama-3.1-8b-instruct",), api_key="nv_y"),
    )
    assert logger.warnings == []


def test_an_unknown_provider_type_still_fails_fast():
    """Relaxing the credential check must not relax the configuration validation."""
    with pytest.raises(ValueError, match="unknown provider type"):
        build_registry(make_provider_config("weird", models=("m",), type="not-a-type"))


# --- F3: non-ASCII inbound key ---------------------------------------------

def test_compare_digest_still_rejects_non_ascii_str():
    """Pins the stdlib behaviour the fix works around; if it changes, we want to know."""
    with pytest.raises(TypeError):
        hmac.compare_digest("tokèn", "secret")


@pytest.mark.parametrize("token", ["tokèn", "日本語", "clé-secrète", "🔑"])
def test_non_ascii_token_is_rejected_with_401_not_500(app_factory, token):
    client = app_factory(proxy_api_key="secret").test_client()
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.get_json()["error"]["type"] == "authentication_error"


def test_non_ascii_token_via_x_api_key_is_also_a_401(app_factory):
    client = app_factory(proxy_api_key="secret").test_client()
    assert client.get("/v1/models", headers={"X-Api-Key": "tokèn"}).status_code == 401


def test_a_non_ascii_key_can_itself_be_the_configured_secret(app_factory):
    """The fix must work in both directions: the *configured* key may be non-ASCII too."""
    client = app_factory(proxy_api_key="chiave-segretà").test_client()
    assert client.get(
        "/v1/models", headers={"Authorization": "Bearer chiave-segretà"}
    ).status_code == 200
    assert client.get(
        "/v1/models", headers={"Authorization": "Bearer chiave-segreta"}
    ).status_code == 401


def test_a_valid_ascii_token_is_still_accepted(app_factory):
    client = app_factory(proxy_api_key="secret").test_client()
    assert client.get(
        "/v1/models", headers={"Authorization": "Bearer secret"}
    ).status_code == 200


def test_health_stays_exempt_from_authentication(app_factory):
    client = app_factory(proxy_api_key="secret").test_client()
    assert client.get("/health").status_code == 200
