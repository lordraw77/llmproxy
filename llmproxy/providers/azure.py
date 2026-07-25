"""Azure OpenAI provider.

Same request/response *shape* as OpenAI, but three deployment-specific twists:
authentication is the ``api-key`` header (set via config), the deployment name
lives in the URL path, and an ``api-version`` query parameter is mandatory. Only
the URL differs, so everything else is inherited from
:class:`~llmproxy.providers.openai_compatible.OpenAICompatibleProvider`.

``base_url`` is the resource root, e.g. ``https://my-res.openai.azure.com``; the
native model id is used as the deployment name.
"""

from urllib.parse import quote

from .openai_compatible import OpenAICompatibleProvider


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """OpenAI-shaped upstream served through Azure's deployment-scoped URLs."""

    def _url(self, path, stream, model=None):
        base = self._config.base_url.rstrip("/")
        deployment = quote(model or "", safe="")
        version = self._config.api_version or "2024-02-01"
        return f"{base}/openai/deployments/{deployment}{path}?api-version={version}"

    def health(self, timeout):
        base = self._config.base_url.rstrip("/")
        version = self._config.api_version or "2024-02-01"
        return self._session.get(
            f"{base}/openai/models?api-version={version}", headers=self._headers, timeout=timeout,
        )
