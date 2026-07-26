"""Regression tests for F7 — routing refusals are 400 JSON, not 500 HTML.

``register_error_handlers`` only handled ``RequestException``, so every
``ValueError`` raised while routing a request reached the client as Flask's HTML
500 page:

- ``AnthropicProvider._url`` / ``GeminiProvider._url`` — no embeddings endpoint;
- ``ProviderRegistry.embeddings_provider_for`` — unknown embeddings model;
- ``ProviderRegistry.provider_for`` — which did not even raise: it dereferenced
  ``None`` and produced an ``AttributeError`` on an empty catalogue.

All of these are client errors and must be 400 in the OpenAI error format.
"""

import pytest
import requests

from llmproxy.providers.base import AggregatedResponse
from llmproxy.providers.openai_compatible import OpenAICompatibleProvider
from llmproxy.providers.registry import ProviderRegistry

from .conftest import make_provider_config


def error_of(resp):
    return resp.get_json()["error"]


# --- the registry guard ----------------------------------------------------

def test_provider_for_raises_instead_of_dereferencing_none():
    """An empty catalogue used to give ``AttributeError: 'NoneType' has no 'provider'``."""
    registry = ProviderRegistry.build([], {})
    with pytest.raises(ValueError, match="no provider serves chat model"):
        registry.provider_for("anything")


def test_provider_for_still_resolves_a_known_model():
    import logging

    from llmproxy.providers import build_providers

    from .conftest import make_settings

    registry = build_providers(
        make_settings(providers=(make_provider_config("groq", models=("llama-3.3-70b",)),)),
        logging.getLogger("test-silent"),
    )
    provider, native = registry.provider_for("llama-3.3-70b")
    assert provider.name == "groq"
    assert native == "llama-3.3-70b"


# --- the HTTP surface ------------------------------------------------------

@pytest.fixture
def empty_registry_client(app_factory):
    """A client over an app whose provider exposes no model at all."""
    app = app_factory(providers=(make_provider_config("empty"),))
    return app.test_client()


@pytest.mark.parametrize("path,payload", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/completions", {"prompt": "hi"}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
    ("/completion", {"prompt": "hi"}),
])
def test_an_empty_catalogue_answers_400_not_500(empty_registry_client, path, payload):
    resp = empty_registry_client.post(path, json={"model": "nope", "stream": False, **payload})

    assert resp.status_code == 400
    assert resp.mimetype == "application/json"
    assert error_of(resp)["type"] == "invalid_request_error"


@pytest.mark.parametrize("path,payload", [
    ("/v1/embeddings", {"input": "hi"}),
    ("/api/embeddings", {"prompt": "hi"}),
    ("/api/embed", {"input": "hi"}),
])
def test_an_unknown_embeddings_model_answers_400(app_factory, path, payload):
    client = app_factory().test_client()
    resp = client.post(path, json={"model": "not-a-model", **payload})

    assert resp.status_code == 400
    assert "not-a-model" in error_of(resp)["message"]
    assert error_of(resp)["type"] == "invalid_request_error"


def test_a_provider_without_an_embeddings_endpoint_answers_400(app_factory):
    """Anthropic and Gemini raise from ``_url``: a capability refusal, not a bug."""
    gemini = make_provider_config("gemini", models=("gemini-2.0-flash",),
                                  embeddings_models=("gemini-embed",), type="gemini")
    client = app_factory(providers=(gemini,)).test_client()

    resp = client.post("/v1/embeddings", json={"model": "gemini-embed", "input": "hi"})

    assert resp.status_code == 400
    assert "embeddings" in error_of(resp)["message"]


# --- the handler must not swallow the upstream-error path ------------------

def test_an_upstream_failure_is_still_a_502_not_a_400(app_factory, monkeypatch):
    def boom(self, payload, stream, rid, path="/chat/completions"):
        raise requests.exceptions.ConnectionError("upstream unreachable")

    monkeypatch.setattr(OpenAICompatibleProvider, "post", boom)
    client = app_factory().test_client()
    resp = client.post("/v1/chat/completions",
                       json={"model": "test-model", "stream": False,
                             "messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code == 502
    assert error_of(resp)["type"] == "upstream_request_error"


def test_a_non_json_upstream_body_stays_on_the_upstream_handler():
    """``requests`` raises a JSONDecodeError that is *also* a RequestException.

    Flask walks the exception MRO, where ``RequestException`` precedes
    ``ValueError``, so a broken upstream body is not misreported as a client error.
    """
    assert requests.exceptions.JSONDecodeError.__mro__.index(requests.exceptions.RequestException) \
        < requests.exceptions.JSONDecodeError.__mro__.index(ValueError)


def test_a_valid_request_is_unaffected(app_factory, monkeypatch):
    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        return AggregatedResponse({
            "id": "x", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
        })

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)
    client = app_factory().test_client()
    resp = client.post("/v1/chat/completions",
                       json={"model": "test-model", "stream": False,
                             "messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code == 200
