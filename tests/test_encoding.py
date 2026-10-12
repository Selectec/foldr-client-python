"""Unexpected content encoding cannot bypass streaming memory boundaries."""

import gzip

import httpx
import pytest

from foldr_client import FoldrClient, FoldrError, FoldrOAuth, OAuthError, ProtocolError

SITE = "https://files.example.com"


class TrackedBody(httpx.SyncByteStream):
    def __init__(self, data, *, read_ceiling=None):
        self.data = data
        self.read_ceiling = read_ceiling
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.data), 4096):
            chunk = self.data[offset : offset + 4096]
            self.delivered += len(chunk)
            if self.read_ceiling is not None and self.delivered > self.read_ceiling:
                pytest.fail("Error handling eagerly consumed the response")
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_encoded_success_is_rejected_before_reading_and_closed(encoding):
    body = TrackedBody(gzip.compress(b"x" * (1024 * 1024)))

    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=body)

    with FoldrClient(SITE, access_token="fake", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProtocolError):
            with client.stream_content("share", "/compressed"):
                pytest.fail("Unexpected encoding must be rejected before exposing content")
    assert body.delivered == 0
    assert body.closed


def test_compressed_error_preserves_small_raw_body_without_expanding_it():
    # This small encoded body expands far beyond the error-body cap if decoded.
    compressed = gzip.compress(b"x" * (16 * 1024 * 1024))
    assert len(compressed) < 65536
    body = TrackedBody(compressed)

    def handle(request):
        return httpx.Response(503, headers={"Content-Encoding": "gzip"}, stream=body)

    with FoldrClient(SITE, access_token="fake", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FoldrError) as caught:
            with client.stream_content("share", "/compressed"):
                pytest.fail("An error status must not expose a content stream")
    assert caught.value.status_code == 503
    assert caught.value.body == compressed
    assert caught.value.problem is None
    assert caught.value.body_truncated is False
    assert body.closed


def test_large_compressed_error_stops_reading_at_the_raw_body_cap():
    member = gzip.compress(b"x" * (1024 * 1024))
    compressed = member * 1000
    body = TrackedBody(compressed, read_ceiling=65536 + 8192)

    def handle(request):
        return httpx.Response(503, headers={"Content-Encoding": "gzip"}, stream=body)

    with FoldrClient(SITE, access_token="fake", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FoldrError) as caught:
            with client.stream_content("share", "/compressed"):
                pytest.fail("An error status must not expose a content stream")
    assert caught.value.body == compressed[:65536]
    assert caught.value.body_truncated is True
    assert caught.value.problem is None
    assert body.delivered < len(compressed)
    assert body.closed


def test_oauth_compressed_error_is_raw_and_closed():
    compressed = gzip.compress(b'{"error":"server_error"}')
    body = TrackedBody(compressed)

    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(500, headers={"Content-Encoding": "gzip"}, stream=body)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(OAuthError) as caught:
            oauth.refresh("fdr_rt_fake")
    assert caught.value.body == compressed
    assert caught.value.problem is None
    assert caught.value.outcome_uncertain is True
    assert body.closed
