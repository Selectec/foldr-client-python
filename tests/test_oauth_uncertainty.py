"""Malformed token successes remain unsafe to replay and never install tokens."""

import httpx
import pytest

from foldr_client import FoldrOAuth, ProtocolError

SITE = "https://files.example.com"


class TokenBody(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content
        self.closed = False

    def __iter__(self):
        yield self.content

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"access_token":"fake"}',
        b'{"access_token":"fake","token_type":"unsupported"}',
    ],
)
def test_malformed_token_success_is_explicitly_uncertain_and_closed(content):
    calls = []
    body = TokenBody(content)

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(ProtocolError) as caught:
            oauth.refresh("fdr_rt_fake")
    assert caught.value.outcome_uncertain is True
    assert len(calls) == 1
    assert body.closed


@pytest.mark.parametrize("replacement", [None, "", 123])
def test_refresh_requires_nonempty_replacement_refresh_token(replacement):
    calls = []

    def handle(request):
        calls.append(request)
        tokens = {"access_token": "fdr_at_new", "token_type": "Bearer", "scope": "foldr.read"}
        if replacement is not None:
            tokens["refresh_token"] = replacement
        return httpx.Response(200, json=tokens)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(ProtocolError) as caught:
            oauth.refresh("fdr_rt_fake")
    assert caught.value.outcome_uncertain is True
    assert len(calls) == 1
