"""Download contracts exercised with lazy, interruptible in-memory HTTP streams."""

import httpx
import pytest

from foldr_client import FoldrClient, FoldrError, ProtocolError, StreamError

SITE = "https://foldr.example"


class FakeStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, interrupt=False):
        self.chunks = chunks
        self.interrupt = interrupt
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.interrupt:
            raise httpx.ReadError("connection interrupted at private signed URL")

    def close(self):
        self.closed = True


def test_stream_is_lazy_bounded_and_closed_on_early_exit():
    source = FakeStream([b"abcdefgh"] * 10000)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, stream=source, headers={"Content-Type": "application/octet-stream"}
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with client.stream_content("s", "/Large File.bin", direct=False) as content:
            assert source.reads == 0
            assert content.bytes_received == 0
            iterator = content.iter_bytes(chunk_size=4)
            assert source.reads == 0
            assert next(iterator) == b"abcd"
            assert content.bytes_received == 4
            assert source.reads == 1
            assert source.closed is False
        assert source.closed is True
    assert dict(requests[0].url.params) == {"path": "/Large File.bin", "direct": "false"}
    assert requests[0].headers["accept-encoding"] == "identity"


def test_complete_stream_preserves_bytes_with_bounded_output_chunks():
    source = FakeStream([b"0123456789", b"abcdefghijk", b"lmnop"])
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source)),
    ) as client:
        with client.stream_content("s", "/f") as content:
            chunks = list(content.iter_bytes(chunk_size=3))
            assert all(0 < len(chunk) <= 3 for chunk in chunks)
            assert b"".join(chunks) == b"0123456789abcdefghijklmnop"
            assert content.bytes_received == 26
    assert source.closed


def test_interrupted_stream_reports_delivered_bytes_without_replay():
    source = FakeStream([b"abcd", b"efgh"], interrupt=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=source)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with client.stream_content("s", "/f") as content:
            iterator = content.iter_bytes(chunk_size=4)
            assert next(iterator) == b"abcd"
            assert next(iterator) == b"efgh"
            with pytest.raises(StreamError) as caught:
                next(iterator)
            assert caught.value.bytes_received == 8
            assert content.bytes_received == 8
            assert "private signed URL" not in str(caught.value)
    assert len(requests) == 1
    assert source.closed


def test_download_context_preserves_caller_http_errors_and_closes_response():
    source = FakeStream([b"file bytes"])
    caller_error = httpx.ReadTimeout("Unrelated caller request failed")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=source)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout) as caught:
            with client.stream_content("s", "/f"):
                raise caller_error
    assert caught.value is caller_error
    assert source.closed
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (206, b"def", {"Content-Range": "bytes 3-5/9", "Accept-Ranges": "bytes"}),
        (200, b"abcdefghi", {"Accept-Ranges": "none"}),
    ],
)
def test_ranges_preserve_actual_status_and_content_when_storage_ignores_range(
    status, body, headers
):
    requests = []
    source = FakeStream([body])

    def handler(request):
        requests.append(request)
        return httpx.Response(status, stream=source, headers=headers)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with client.stream_content("s", "/f", byte_range="bytes=3-5") as content:
            assert content.status_code == status
            assert content.headers["accept-ranges"] == headers["Accept-Ranges"]
            assert content.headers.get("content-range") == headers.get("Content-Range")
            assert b"".join(content.iter_bytes()) == body
    assert requests[0].headers["range"] == "bytes=3-5"
    assert source.closed


def test_native_document_export_uses_content_endpoint_and_preserves_export_headers():
    requests = []
    source = FakeStream([b"exported-document-bytes"])
    mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    disposition = "attachment; filename*=UTF-8''Minutes.docx"

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            stream=source,
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": disposition,
                "Accept-Ranges": "none",
            },
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with client.stream_content("gdrive", "/Minutes", byte_range="bytes=0-10") as content:
            assert content.status_code == 200
            assert content.headers["content-type"] == mime_type
            assert content.headers["content-disposition"] == disposition
            assert "content-length" not in content.headers
            assert b"".join(content.iter_bytes()) == b"exported-document-bytes"
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/shares/gdrive/content"
    assert requests[0].url.params["path"] == "/Minutes"


def test_conditional_download_exposes_304_without_reading_or_inventing_file_bytes():
    requests = []
    source = FakeStream([])

    def handler(request):
        requests.append(request)
        return httpx.Response(304, stream=source, headers={"ETag": '"version-1"'})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with client.stream_content("s", "/f", if_none_match='"version-1"') as content:
            assert content.status_code == 304
            assert content.headers["etag"] == '"version-1"'
            assert list(content.iter_bytes()) == []
    assert requests[0].headers["if-none-match"] == '"version-1"'
    assert source.closed


@pytest.mark.parametrize(
    "destinations",
    [
        ["https://storage.example/blob?signature=private"],
        ["https://foldr.example/proxy?signature=private"],
        [
            "https://foldr.example/proxy",
            "https://storage.example/blob",
            "https://foldr.example/final",
        ],
    ],
)
def test_every_redirect_hop_removes_foldr_auth_and_cookies_even_on_same_origin(destinations):
    requests = []
    streams = []

    def handler(request):
        requests.append(request)
        source = FakeStream([b"bytes"] if len(requests) > len(destinations) else [])
        streams.append(source)
        headers = {"Set-Cookie": "session=private; Path=/"}
        if len(requests) <= len(destinations):
            headers["Location"] = destinations[len(requests) - 1]
            return httpx.Response(302, headers=headers, stream=source)
        return httpx.Response(200, headers=headers, stream=source)

    with FoldrClient(
        SITE, access_token="foldr-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with client.stream_content("s", "/f", byte_range="bytes=0-4") as content:
            assert b"".join(content.iter_bytes()) == b"bytes"
    assert requests[0].headers["authorization"] == "Bearer foldr-secret"
    assert requests[0].headers.get("cookie") is None
    assert [str(request.url) for request in requests[1:]] == destinations
    assert all("authorization" not in request.headers for request in requests[1:])
    assert all("cookie" not in request.headers for request in requests[1:])
    assert all(request.headers["range"] == "bytes=0-4" for request in requests)
    assert all(source.closed for source in streams)


@pytest.mark.parametrize(
    "target",
    [
        "http://storage.example/blob",
        "https://user:pass@storage.example/blob",
        "file:///private/file",
    ],
)
def test_unsafe_content_redirects_are_rejected_before_contacting_target(target):
    requests = []
    source = FakeStream([])

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": target}, stream=source)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            with client.stream_content("s", "/f"):
                pytest.fail("Unsafe redirect must not open a content stream")
    assert len(requests) == 1
    assert source.closed


def test_redirect_loop_is_bounded_and_closes_every_response():
    sources = []

    def handler(request):
        source = FakeStream([])
        sources.append(source)
        return httpx.Response(302, headers={"Location": "/again"}, stream=source)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            with client.stream_content("s", "/f"):
                pytest.fail("Redirect loop must fail")
    assert len(sources) == 6  # The initial request plus at most five redirect requests.
    assert all(source.closed for source in sources)


@pytest.mark.parametrize(
    "status,code",
    [
        (403, "download-blocked"),
        (409, "not-a-file"),
        (409, "not-downloadable"),
    ],
)
def test_foldr_download_decisions_surface_without_storage_requests(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"code": code, "provider": {"detail": "policy"}})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FoldrError) as caught:
            with client.stream_content("s", "/f"):
                pytest.fail("Rejected content must not produce a stream")
    assert caught.value.status_code == status
    assert caught.value.code == code
    assert caught.value.problem["provider"] == {"detail": "policy"}
    assert len(requests) == 1


def test_large_error_body_is_bounded_and_closed():
    source = FakeStream([b"x" * 8192] * 10000)
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(502, stream=source)),
    ) as client:
        with pytest.raises(FoldrError) as caught:
            with client.stream_content("s", "/f"):
                pytest.fail("Server failure must not open a content stream")
    assert len(caught.value.body) == 65536
    assert caught.value.body_truncated is True
    assert caught.value.problem is None
    assert source.reads < 20
    assert source.closed


def test_nonpositive_stream_chunk_size_rejected_and_response_still_closed():
    source = FakeStream([b"abc"])
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source)),
    ) as client:
        with client.stream_content("s", "/f") as content:
            with pytest.raises(ValueError):
                list(content.iter_bytes(chunk_size=0))
            assert source.reads == 0
    assert source.closed
