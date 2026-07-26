"""Request-side translation from the OpenAI dialect to native provider formats.

The response direction already lives next to each provider (``_parts_to_message``,
``_to_openai_message``): it is a pure function of the upstream body and is
exercised by the normalization tests. The request direction is the harder half —
it has to carry a *conversation* across dialects, including the tool-calling
round-trip — so it lives here, free of HTTP, and is tested without a network.
"""
