"""Provider registry: the exposed-model catalogue and the routing rules.

Aggregates the models of every configured provider into a single flat catalogue
and knows, for any exposed model name, which provider serves it and under what
**native** id. Replaces the single-upstream ``ModelRegistry``.

Naming rule (v1.3.0): the exposed name of a provider model is its configured
``alias`` when set; otherwise it is the bare ``model_id`` when a **single**
provider is configured (identical to the pre-multi-provider behaviour), or
``f"{provider}:{model_id}"`` when **two or more** providers coexist and names must
be disambiguated. Two models resolving to the same exposed name is a configuration
error and fails fast at start-up.
"""


class _Entry:
    """One exposed model: which provider serves it and its native upstream id."""

    __slots__ = ("exposed", "provider", "model_id")

    def __init__(self, exposed, provider, model_id):
        self.exposed = exposed
        self.provider = provider
        self.model_id = model_id


class ProviderRegistry:
    """Flat catalogue of exposed models across every provider, plus routing."""

    def __init__(self, providers, chat_entries, embedding_entries):
        self.providers = tuple(providers)
        self._chat = {e.exposed: e for e in chat_entries}
        self._embeddings = {e.exposed: e for e in embedding_entries}
        # Bare native id -> exposed name, for back-compat resolution of clients
        # that still send the unprefixed model id (e.g. after the NVIDIA_* env
        # fallback). First declaration wins on duplicates.
        self._bare = {}
        for e in chat_entries:
            self._bare.setdefault(e.model_id, e.exposed)
        self.models = tuple(e.exposed for e in chat_entries)
        self.default_model = self.models[0] if self.models else ""
        self.embeddings_model = embedding_entries[0].exposed if embedding_entries else ""

    # --- chat -------------------------------------------------------------

    def has(self, name):
        """Whether ``name`` is an exposed chat model."""
        return name in self._chat

    def resolve(self, requested):
        """Resolve a requested model to its canonical exposed name.

        Accepts an exact exposed name/alias/``provider:model``, or a bare native
        model id (unambiguous match, else the first-declared owner). Falls back to
        the default model when nothing matches.
        """
        if requested and requested in self._chat:
            return requested
        if requested and requested in self._bare:
            return self._bare[requested]
        return self.default_model

    def provider_for(self, name):
        """Return ``(provider, native_model_id)`` for an exposed chat model name."""
        entry = self._chat.get(name) or self._chat.get(self.resolve(name))
        return entry.provider, entry.model_id

    # --- embeddings -------------------------------------------------------

    def resolve_embeddings(self, requested):
        """Resolve a requested embeddings model to its exposed name."""
        if requested and requested in self._embeddings:
            return requested
        for exposed, entry in self._embeddings.items():
            if entry.model_id == requested:
                return exposed
        return requested or self.embeddings_model

    def embeddings_provider_for(self, name):
        """Return ``(provider, native_model_id)`` for an exposed embeddings model."""
        entry = self._embeddings.get(name)
        if entry is None:
            resolved = self.resolve_embeddings(name)
            entry = self._embeddings.get(resolved)
        if entry is None:
            raise ValueError(f"no provider serves embeddings model '{name}'")
        return entry.provider, entry.model_id

    # --- construction -----------------------------------------------------

    @classmethod
    def build(cls, providers, model_specs):
        """Build the registry from instantiated providers and their model specs.

        Args:
            providers: ordered iterable of :class:`~llmproxy.providers.base.Provider`.
            model_specs: mapping ``provider_name -> {"chat": [(id, alias)],
                "embeddings": [(id, alias)]}``.

        Raises:
            ValueError: if two models collide on the same exposed name.
        """
        providers = list(providers)
        chat_entries, embedding_entries = [], []
        seen = {}
        # With a single provider there is nothing to disambiguate, so exposed
        # names stay bare (identical to the pre-multi-provider behaviour). The
        # "<provider>:<model>" prefix kicks in only when 2+ providers coexist.
        prefixed = len(providers) > 1
        for provider in providers:
            spec = model_specs.get(provider.name, {})
            for kind, bucket in (("chat", chat_entries), ("embeddings", embedding_entries)):
                for model_id, alias in spec.get(kind, []):
                    exposed = alias or (f"{provider.name}:{model_id}" if prefixed else model_id)
                    if exposed in seen:
                        raise ValueError(
                            f"model name collision: '{exposed}' is exposed by both "
                            f"'{seen[exposed]}' and '{provider.name}' (set a distinct alias)"
                        )
                    seen[exposed] = provider.name
                    bucket.append(_Entry(exposed, provider, model_id))
        return cls(providers, chat_entries, embedding_entries)
