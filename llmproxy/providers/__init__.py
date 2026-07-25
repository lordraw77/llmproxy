"""Provider layer: the upstream abstraction and its implementations.

The rest of the app depends only on :func:`build_providers` (to construct the
registry) and on the uniform OpenAI-shaped contract every
:class:`~llmproxy.providers.base.Provider` exposes.
"""

from .base import AggregatedResponse, Provider, resp_json
from .factory import build_providers
from .registry import ProviderRegistry

__all__ = [
    "AggregatedResponse",
    "Provider",
    "ProviderRegistry",
    "build_providers",
    "resp_json",
]
