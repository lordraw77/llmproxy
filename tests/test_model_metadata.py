"""Tests for R6 — discovery endpoints must name the provider that serves a model.

``owned_by`` (OpenAI) and ``details.family`` (Ollama) were the constant
``"nvidia"``, so with any second provider configured a Claude or Gemini model was
announced as NVIDIA's. Clients group their model picker by these fields, so the
value is visible rather than cosmetic.
"""

from .conftest import make_provider_config


def two_providers():
    """A Groq and a Gemini provider, each serving one model under an alias."""
    return (
        make_provider_config("groq", models=[("llama-3.3-70b", "fast")]),
        make_provider_config("gemini", models=[("gemini-2.0-flash", "smart")],
                             embeddings_models=[("text-embedding-004", "vectors")]),
    )


# --- registry accessor -----------------------------------------------------

def test_owner_of_names_the_serving_provider(app_factory):
    registry = app_factory(providers=two_providers()).extensions["llmproxy"].registry

    assert registry.owner_of("fast") == "groq"
    assert registry.owner_of("smart") == "gemini"


def test_owner_of_covers_embeddings_models(app_factory):
    registry = app_factory(providers=two_providers()).extensions["llmproxy"].registry

    assert registry.owner_of("vectors") == "gemini"


def test_owner_of_falls_back_for_an_unknown_model(app_factory):
    """There is no upstream to attribute a model nobody serves to."""
    registry = app_factory(providers=two_providers()).extensions["llmproxy"].registry

    assert registry.owner_of("nope") == "llmproxy"


# --- OpenAI discovery ------------------------------------------------------

def test_v1_models_reports_the_owner_per_model(app_factory):
    data = app_factory(providers=two_providers()).test_client().get("/v1/models").get_json()

    owners = {entry["id"]: entry["owned_by"] for entry in data["data"]}
    assert owners == {"fast": "groq", "smart": "gemini"}


def test_v1_model_detail_reports_the_owner(app_factory):
    entry = app_factory(providers=two_providers()).test_client().get("/v1/models/smart").get_json()

    assert entry["owned_by"] == "gemini"


def test_a_single_provider_still_reports_its_own_name(client):
    """The default fixture's provider is named "test", not "nvidia"."""
    data = client.get("/v1/models").get_json()

    assert {e["owned_by"] for e in data["data"]} == {"test"}


# --- Ollama discovery ------------------------------------------------------

def test_api_tags_reports_the_family_per_model(app_factory):
    data = app_factory(providers=two_providers()).test_client().get("/api/tags").get_json()

    families = {m["name"]: m["details"]["family"] for m in data["models"]}
    assert families == {"fast": "groq", "smart": "gemini"}


def test_api_show_reports_the_family_of_the_requested_model(app_factory):
    app = app_factory(providers=two_providers())

    data = app.test_client().post("/api/show", json={"model": "smart"}).get_json()
    assert data["details"]["family"] == "gemini"

    data = app.test_client().post("/api/show", json={"model": "fast"}).get_json()
    assert data["details"]["family"] == "groq"


def test_api_show_accepts_the_ollama_name_field(app_factory):
    """Ollama's own client sends `name`; some forks send `model`."""
    app = app_factory(providers=two_providers())

    data = app.test_client().post("/api/show", json={"name": "smart"}).get_json()
    assert data["details"]["family"] == "gemini"


def test_api_show_falls_back_to_the_default_model(app_factory):
    """An empty or unknown body resolves to the default, as inference does."""
    app = app_factory(providers=two_providers())

    for body in ({}, {"model": "unknown"}):
        data = app.test_client().post("/api/show", json=body).get_json()
        assert data["details"]["family"] == "groq", "the default model is Groq's"


def test_api_show_survives_a_missing_body(app_factory):
    app = app_factory(providers=two_providers())

    resp = app.test_client().post("/api/show", data=b"", content_type="application/json")

    assert resp.status_code == 200
    assert resp.get_json()["details"]["family"] == "groq"
