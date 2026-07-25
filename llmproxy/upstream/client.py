"""Backward-compatibility shim.

The upstream HTTP client is now the provider layer: ``NvidiaUpstream`` has been
superseded by :class:`llmproxy.providers.openai_compatible.OpenAICompatibleProvider`.
This module re-exports the response helpers from :mod:`llmproxy.providers.base` so
existing ``from llmproxy.upstream.client import resp_json`` imports keep working.
"""

from ..providers.base import AggregatedResponse, resp_json

__all__ = ["AggregatedResponse", "resp_json"]
