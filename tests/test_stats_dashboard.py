"""Tests for the ``/stats`` dashboard after the move to a Jinja template (R8).

The refactor is meant to be behaviour-preserving, so these pin the two things
that could silently change: the shape the template is fed (ordering, duration
formatting, which cards appear) and the escaping that :mod:`F1` depends on —
which is now Jinja's autoescaping rather than a hand-applied ``html.escape``.
"""

import pytest

from llmproxy.web.routes.stats import _sorted_pairs, _template_context, format_uptime


# --- data shaping ----------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (9, "9s"),
    (61, "1m 1s"),
    (3600, "1h 0m 0s"),
    (3661, "1h 1m 1s"),
    (90061, "1d 1h 1m 1s"),
    (59.9, "59s"),  # truncated, not rounded
])
def test_format_uptime(seconds, expected):
    assert format_uptime(seconds) == expected


def test_sorted_pairs_orders_by_count_then_key():
    pairs = _sorted_pairs({"/b": 1, "/a": 5, "/c": 5})
    assert pairs == [("/a", 5), ("/c", 5), ("/b", 1)]


def test_sorted_pairs_tolerates_an_empty_or_missing_mapping():
    assert _sorted_pairs({}) == []
    assert _sorted_pairs(None) == []


def _payload(**metrics):
    base = {
        "requests": {"total": 1, "in_flight": 0, "errors": 0, "by_status": {}, "by_path": {}},
        "latency_ms": {"avg": 1.0, "max": 2.0, "count": 1},
        "tokens": {"prompt": 1, "completion": 2, "total": 3},
        "upstream": {"calls": 1, "errors": 0, "avg_latency_ms": 1.0, "max_latency_ms": 2.0},
        "uptime_seconds": 5,
        "started_at": "2026-07-26T00:00:00Z",
    }
    base.update(metrics)
    return {
        "metrics": base,
        "process": {"pid": 1},
        "models": {"exposed": [], "default": "", "embeddings": "", "providers": []},
    }


def test_template_context_derives_the_cache_hit_rate_percentage():
    cache = {"hit_rate": 0.3333, "enabled": True}
    ctx = _template_context(_payload(cache=cache))
    assert ctx["hit_rate_pct"] == 33.3  # one decimal, as the f-string renderer did


def test_template_context_leaves_the_cache_unset_when_there_is_none():
    ctx = _template_context(_payload())
    assert ctx["cache"] is None
    assert ctx["hit_rate_pct"] is None


# --- rendering -------------------------------------------------------------

def test_the_cache_card_is_omitted_when_no_cache_is_wired(app_factory):
    app = app_factory()
    app.extensions["llmproxy"].cache = None
    html = app.test_client().get("/stats").get_data(as_text=True)

    assert "<h2>Response cache</h2>" not in html
    assert "<h2>Requests</h2>" in html


def test_empty_metric_tables_render_a_placeholder_row(client):
    """A fresh worker has no statuses yet; the table must not collapse."""
    html = client.get("/stats").get_data(as_text=True)
    assert "—" in html


def test_the_template_escapes_a_hostile_value_by_construction(app_factory):
    """Autoescaping, not the F1 by_path bucketing, is what makes this safe.

    The value is injected straight into a metric group the template renders, so
    nothing upstream of Jinja can be credited with sanitizing it.
    """
    app = app_factory()
    collector = app.extensions["llmproxy"].metrics
    snapshot = collector.snapshot

    def hostile_snapshot():
        data = snapshot()
        data["requests"]["by_status"] = {"<script>alert(1)</script>": 1}
        return data

    collector.snapshot = hostile_snapshot
    html = app.test_client().get("/stats").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_dashboard_declares_its_own_refresh_interval(client):
    from llmproxy.web.routes.stats import REFRESH_SECONDS

    html = client.get("/stats").get_data(as_text=True)
    assert f'http-equiv="refresh" content="{REFRESH_SECONDS}"' in html
    assert f"auto-refresh {REFRESH_SECONDS}s" in html


def test_the_dashboard_is_served_as_html(client):
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert resp.get_data(as_text=True).lstrip().startswith("<!doctype html>")
