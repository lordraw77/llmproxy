"""SSE streams are UTF-8 even when the upstream omits the charset."""

import io

import requests

from llmproxy.upstream.sse import iter_openai_sse


def test_bare_event_stream_is_decoded_as_utf8():
    resp = requests.Response()
    resp.status_code = 200
    resp.headers["Content-Type"] = "text/event-stream"
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)  # ISO-8859-1
    body = 'data: {"choices":[{"delta":{"content":"De André, città"}}]}\n\ndata: [DONE]\n\n'
    resp.raw = io.BytesIO(body.encode("utf-8"))

    assert "".join(iter_openai_sse(resp)) == "De André, città"
