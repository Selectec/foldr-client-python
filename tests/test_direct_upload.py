"""Direct storage uses its own session credentials and bounded request streams."""

import io

import httpx
import pytest

from foldr_client.errors import FoldrError, ProtocolError, TransportError
from foldr_client.uploads import DirectUpload


def session(**overrides):
    value = {
        "id": "session-1",
        "url": "https://storage.example/upload?signature=private",
        "method": "PUT",
        "headers": {"Content-Type": "video/quicktime", "X-Storage-Proof": "signed"},
        "size": 5,
        "chunk_size": None,
    }
    value.update(overrides)
    return value


def test_whole_upload_preserves_storage_contract_and_ignores_nonjson_response():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "PUT"
        assert request.url == "https://storage.example/upload?signature=private"
        assert request.headers["content-type"] == "video/quicktime"
        assert request.headers["x-storage-proof"] == "signed"
        assert request.headers["content-length"] == "5"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "transfer-encoding" not in request.headers
        assert request.read() == b"hello"
        return httpx.Response(201, headers={"ETag": '"stored"'}, content=b"storage text")

    with DirectUpload(session(), transport=httpx.MockTransport(handler)) as upload:
        response = upload.send(b"hello", content_length=5)
    assert len(seen) == 1
    assert response.status_code == 201
    assert response.headers["etag"] == '"stored"'
    assert response.data is None


def test_supplied_storage_auth_is_preserved_but_cookies_are_not_retained():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer storage-only"
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"Set-Cookie": "storage_cookie=secret; Path=/"})

    with DirectUpload(
        session(headers={"Authorization": "Bearer storage-only"}),
        transport=httpx.MockTransport(handler),
    ) as upload:
        upload.send(b"hello", content_length=5)
        upload.send(b"hello", content_length=5)
    assert len(seen) == 2


def test_chunked_upload_accepts_intermediate_and_final_pieces():
    seen = []

    def handler(request):
        seen.append((request.headers["content-range"], request.read()))
        return httpx.Response(202 if len(seen) == 1 else 201)

    with DirectUpload(session(chunk_size=3), transport=httpx.MockTransport(handler)) as upload:
        first = upload.send(b"hel", content_length=3, content_range="bytes 0-2/5")
        last = upload.send(b"lo", content_length=2, content_range="bytes 3-4/5")
    assert first.status_code == 202
    assert last.status_code == 201
    assert seen == [("bytes 0-2/5", b"hel"), ("bytes 3-4/5", b"lo")]


@pytest.mark.parametrize("status", [200, 201])
def test_final_storage_statuses(status):
    with DirectUpload(
        session(), transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as upload:
        assert upload.send(b"hello", content_length=5).status_code == status


@pytest.mark.parametrize(
    "content_range,length",
    [
        (None, 3),
        ("bytes 0-2/*", 3),
        ("bytes 0-2/6", 3),
        ("bytes 1-3/5", 3),
        ("bytes 0-1/5", 2),
        ("bytes 0-2/5", 2),
        ("bytes 3-5/5", 3),
        ("bytes 3-2/5", 0),
        ("bytes 0-4/5", 5),
        ("bytes 6-7/5", 2),
    ],
)
def test_invalid_chunk_ranges_are_rejected_before_any_request(content_range, length):
    def handler(request):
        pytest.fail("Invalid range must not reach storage")

    with DirectUpload(session(chunk_size=3), transport=httpx.MockTransport(handler)) as upload:
        with pytest.raises(ValueError):
            upload.send(b"abc", content_length=length, content_range=content_range)


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "http://storage.example/private"},
        {"url": "https://user:secret@storage.example/upload"},
        {"url": "https://storage.example/upload#fragment"},
        {"url": "//storage.example/upload"},
        {"url": None},
        {"method": "PUT\r\nHeader: injected"},
        {"method": None},
        {"headers": []},
        {"headers": {"X": 3}},
        {"size": True},
        {"size": -1},
        {"chunk_size": 0},
        {"chunk_size": True},
    ],
)
def test_invalid_session_metadata(overrides):
    with pytest.raises(ValueError):
        DirectUpload(session(**overrides))


@pytest.mark.parametrize(
    "length,byte_range",
    [(4, None), (5, "bytes 0-4/5"), (True, None), (-1, None)],
)
def test_whole_file_must_match_session(length, byte_range):
    with DirectUpload(session()) as upload:
        with pytest.raises(ValueError):
            upload.send(b"hello", content_length=length, content_range=byte_range)


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "6"},
        {"Content-Range": "bytes 0-4/5"},
        {"Transfer-Encoding": "chunked"},
    ],
)
def test_session_framing_header_conflicts(headers):
    with DirectUpload(session(headers=headers)) as upload:
        with pytest.raises(ValueError):
            upload.send(b"hello", content_length=5)


def test_redirect_is_never_followed_and_error_message_omits_signed_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://elsewhere.example/private"})

    with DirectUpload(session(), transport=httpx.MockTransport(handler)) as upload:
        with pytest.raises(FoldrError) as error:
            upload.send(b"hello", content_length=5)
    assert error.value.status_code == 307
    assert len(seen) == 1
    assert "signature" not in str(error.value)
    assert "elsewhere" not in str(error.value)


def test_rate_limit_is_retained_and_not_retried():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "10"}, text="Slow down")

    with DirectUpload(session(), transport=httpx.MockTransport(handler)) as upload:
        with pytest.raises(FoldrError) as error:
            upload.send(b"hello", content_length=5)
    assert len(seen) == 1
    assert error.value.retry_after == "10"
    assert error.value.body == b"Slow down"


def test_transport_interruption_is_uncertain_and_never_retried():
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.WriteError("secret signed URL", request=request)

    with DirectUpload(session(), transport=httpx.MockTransport(handler)) as upload:
        with pytest.raises(TransportError) as error:
            upload.send(b"hello", content_length=5)
    assert len(seen) == 1
    assert error.value.outcome_uncertain
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status", [200, 201, 204])
def test_intermediate_piece_must_return_202(status):
    with DirectUpload(
        session(chunk_size=3), transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as upload:
        with pytest.raises(ProtocolError) as error:
            upload.send(b"hel", content_length=3, content_range="bytes 0-2/5")
    assert error.value.outcome_uncertain


def test_final_piece_must_return_final_status():
    with DirectUpload(
        session(chunk_size=3), transport=httpx.MockTransport(lambda _: httpx.Response(202))
    ) as upload:
        with pytest.raises(ProtocolError):
            upload.send(b"lo", content_length=2, content_range="bytes 3-4/5")


class StreamingTransport(httpx.BaseTransport):
    """Unlike MockTransport, inspect request chunks before HTTPX buffers them."""

    def __init__(self):
        self.chunks = []
        self.closed = False

    def handle_request(self, request):
        self.chunks = [len(piece) for piece in request.stream]
        return httpx.Response(200)

    def close(self):
        self.closed = True


def test_upload_stream_memory_is_bounded_and_caller_file_stays_open():
    class BoundedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    size = 65536 * 3 + 7
    source = BoundedFile(b"x" * size)
    transport = StreamingTransport()
    with DirectUpload(session(size=size), transport=transport) as upload:
        upload.send(source, content_length=size)
    assert sum(transport.chunks) == size
    assert max(transport.chunks) <= 65536
    assert not source.closed
    assert transport.closed


def test_generator_large_pieces_are_split_to_bounded_chunks():
    size = 65536 * 2 + 3
    transport = StreamingTransport()
    with DirectUpload(session(size=size), transport=transport) as upload:
        upload.send(iter([b"x" * size]), content_length=size)
    assert sum(transport.chunks) == size
    assert max(transport.chunks) <= 65536


def test_empty_whole_file_upload():
    with DirectUpload(
        session(size=0), transport=httpx.MockTransport(lambda _: httpx.Response(201))
    ) as upload:
        assert upload.send(b"", content_length=0).status_code == 201


def test_successful_storage_response_body_is_never_buffered_and_is_closed():
    class UnreadBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("Storage response body is not part of the Foldr upload contract")
            yield b""

        def close(self):
            self.closed = True

    body = UnreadBody()
    with DirectUpload(
        session(), transport=httpx.MockTransport(lambda _: httpx.Response(201, stream=body))
    ) as upload:
        upload.send(b"hello", content_length=5)
    assert body.closed
