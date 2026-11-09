"""``/v1/rerank`` and ``/v1/score``: routed to the provider owning the rerank model."""

import pytest

from llmproxy.providers.openai_compatible import OpenAICompatibleProvider

from .conftest import make_provider_config


class _Resp:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def rerank_client(app_factory, monkeypatch):
    seen = {}

    def fake_post(self, payload, stream, rid, path="/chat/completions"):
        seen.update(payload=payload, path=path, stream=stream)
        return _Resp({"id": "r1", "results": [{"index": 0, "relevance_score": 0.9}]})

    monkeypatch.setattr(OpenAICompatibleProvider, "post", fake_post)
    spark = make_provider_config("spark", rerank_models=(("Qwen-Reranker", "reranker"),))
    client = app_factory(providers=(spark,)).test_client()
    return client, seen


@pytest.mark.parametrize("route,path", [
    ("/v1/rerank", "/rerank"),
    ("/v2/rerank", "/rerank"),
    ("/rerank", "/rerank"),
    ("/v1/score", "/score"),
    ("/score", "/score"),
])
def test_routes_forward_to_the_matching_upstream_path(rerank_client, route, path):
    client, seen = rerank_client
    resp = client.post(route, json={"model": "reranker", "query": "q", "documents": ["a"]})

    assert resp.status_code == 200
    assert seen["path"] == path
    assert seen["payload"]["model"] == "Qwen-Reranker"  # alias -> native id upstream
    assert seen["stream"] is False
    assert resp.get_json()["model"] == "reranker"


def test_model_defaults_to_the_first_rerank_model(rerank_client):
    client, seen = rerank_client
    resp = client.post("/v1/rerank", json={"query": "q", "documents": ["a"]})

    assert resp.status_code == 200
    assert seen["payload"]["model"] == "Qwen-Reranker"


def test_unknown_rerank_model_answers_400_when_none_configured(app_factory):
    client = app_factory().test_client()
    resp = client.post("/v1/rerank", json={"model": "nope", "query": "q", "documents": ["a"]})

    assert resp.status_code == 400
