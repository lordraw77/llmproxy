"""Regression tests for R4 — NO_PROXY is applied to the provider sessions.

``requests`` honors ``NO_PROXY`` only for proxies it reads from the environment.
The proxy configured here is put on ``session.proxies`` explicitly, and from that
point ``select_proxy`` returns it for every URL: the exclusion list was collected
into ``Settings.no_proxy`` and then never consulted, so ``HTTP_PROXY`` alone was
enough to route a provider on ``127.0.0.1`` through the corporate egress proxy.
"""

import dataclasses
import logging

import pytest
from requests.utils import select_proxy

from llmproxy.providers.base import bypasses_proxy
from llmproxy.providers.openai_compatible import OpenAICompatibleProvider

from .conftest import make_provider_config, make_settings


PROXY = "http://egress.corp.invalid:3128"


def build_provider(base_url, **settings_overrides):
    """Build a provider pointed at ``base_url`` with a global egress proxy set."""
    config = dataclasses.replace(make_provider_config("p", models=("m",)), base_url=base_url)
    settings = make_settings(
        providers=(config,), http_proxy=PROXY, https_proxy=PROXY, **settings_overrides
    )
    return OpenAICompatibleProvider(config, settings, logging.getLogger("test"))


def test_a_host_in_no_proxy_is_reached_directly():
    provider = build_provider("http://127.0.0.1:11434/v1", no_proxy="localhost,127.0.0.1")

    assert provider._session.proxies == {}
    assert select_proxy("http://127.0.0.1:11434/v1/chat/completions", {}) is None


def test_a_host_outside_no_proxy_still_goes_through_the_proxy():
    provider = build_provider("https://api.remote.invalid/v1", no_proxy="localhost,127.0.0.1")

    assert provider._session.proxies["https"] == PROXY


def test_without_no_proxy_every_host_goes_through_the_proxy():
    """The unconfigured case must keep behaving exactly as before."""
    provider = build_provider("http://127.0.0.1:11434/v1")

    assert provider._session.proxies["http"] == PROXY


def test_a_provider_level_proxy_also_honours_the_exclusion():
    """An explicit per-provider proxy is not a licence to proxy an excluded host."""
    config = dataclasses.replace(
        make_provider_config("p", models=("m",)),
        base_url="http://127.0.0.1:11434/v1", proxy={"http": PROXY},
    )
    settings = make_settings(providers=(config,), no_proxy="127.0.0.1")
    provider = OpenAICompatibleProvider(config, settings, logging.getLogger("test"))

    assert provider._session.proxies == {}


@pytest.mark.parametrize("url,no_proxy,expected", [
    ("http://127.0.0.1:11434/v1", "localhost,127.0.0.1", True),
    ("http://ollama.internal.example.com/v1", ".internal.example.com", True),
    ("http://10.10.1.5:8000/v1", "10.10.0.0/21", True),
    ("https://api.groq.com/openai/v1", "localhost,127.0.0.1", False),
    ("https://api.groq.com/openai/v1", "", False),
    ("https://api.groq.com/openai/v1", "*", True),
])
def test_matching_rules(url, no_proxy, expected):
    """Host suffixes, bare IPs, CIDR blocks and the wildcard, as documented."""
    assert bypasses_proxy(url, no_proxy) is expected
