"""Storage policies approve every request before consuming or sending bytes."""

import io
import traceback

import httpx
import pytest

from foldr_client import DirectUpload, FoldrClient, ProtocolError

SITE = "https://foldr.example"
STORAGE = "https://storage.example/private-path?signature=secret"


def session(**overrides):
    value = {"url": STORAGE, "method": "PUT", "headers": {}, "size": 5, "chunk_size": None}
    value.update(overrides)
    return value


def fail_policy(url):
    raise RuntimeError(f"Cannot approve {url}")


@pytest.mark.parametrize("policy", [lambda url: False, lambda url: None, fail_policy])
def test_upload_denial_neither_reads_content_nor_sends_request(policy):
    class UnreadFile(io.BytesIO):
        def read(self, size=-1):
            pytest.fail("Denied upload must not read caller content")

    def handler(request):
        pytest.fail("Denied upload must not contact storage")

    source = UnreadFile(b"hello")
    with DirectUpload(
        session(), destination_policy=policy, transport=httpx.MockTransport(handler)
    ) as upload:
        with pytest.raises(ProtocolError) as error:
            upload.send(source, content_length=5)
    assert not source.closed
    assert not error.value.outcome_uncertain
    formatted = "".join(traceback.format_exception(error.value))
    assert STORAGE not in formatted
    assert "private-path" not in formatted
    assert "signature=secret" not in formatted


def test_upload_policy_rechecks_each_piece_before_reading_it():
    destinations = []
    requests = []

    def policy(url):
        assert isinstance(url, httpx.URL)
        destinations.append(url)
        return len(destinations) == 1

    def handler(request):
        requests.append(request)
        assert request.read() == b"hel"
        return httpx.Response(202)

    source = io.BytesIO(b"lo")
    with DirectUpload(
        session(chunk_size=3), destination_policy=policy, transport=httpx.MockTransport(handler)
    ) as upload:
        upload.send(b"hel", content_length=3, content_range="bytes 0-2/5")
        with pytest.raises(ProtocolError):
            upload.send(source, content_length=2, content_range="bytes 3-4/5")
    assert destinations == [httpx.URL(STORAGE), httpx.URL(STORAGE)]
    assert len(requests) == 1
    assert source.tell() == 0


@pytest.mark.parametrize("denied_hop", [1, 2])
def test_download_policy_checks_each_hop_and_closes_denied_redirect(denied_hop):
    requests = []
    checked = []
    bodies = []
    targets = [STORAGE, "https://169.254.169.254/private-path?signature=secret"]

    class UnreadBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("Redirect body must not be consumed")
            yield b""

        def close(self):
            self.closed = True

    def policy(url):
        checked.append(url)
        return len(checked) < denied_hop

    def handler(request):
        requests.append(request)
        assert len(requests) <= denied_hop
        body = UnreadBody()
        bodies.append(body)
        return httpx.Response(302, headers={"Location": targets[len(requests) - 1]}, stream=body)

    with FoldrClient(
        SITE,
        access_token="private",
        destination_policy=policy,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProtocolError) as error:
            with client.stream_content("s", "/f"):
                pytest.fail("Denied destination must not produce content")
    assert len(requests) == denied_hop
    assert checked == [httpx.URL(url) for url in targets[:denied_hop]]
    assert all(body.closed for body in bodies)
    assert "signature" not in str(error.value)
    assert "private-path" not in str(error.value)
    assert requests[0].headers["authorization"] == "Bearer private"
    for request in requests[1:]:
        assert "authorization" not in request.headers


def test_download_policy_failure_is_sanitized_before_storage_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": STORAGE})

    with FoldrClient(
        SITE,
        access_token="private",
        destination_policy=fail_policy,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProtocolError) as error:
            with client.stream_content("s", "/f"):
                pytest.fail("Policy failure must deny the request")
    assert len(requests) == 1
    formatted = "".join(traceback.format_exception(error.value))
    assert STORAGE not in formatted
    assert "signature=secret" not in formatted


@pytest.mark.parametrize("policy", [None, lambda url: url.host == "10.0.0.1"])
def test_private_storage_works_by_default_or_explicit_approval(policy):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://10.0.0.1/file"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"hello")

    with FoldrClient(
        SITE,
        access_token="private",
        destination_policy=policy,
        transport=httpx.MockTransport(handler),
    ) as client:
        with client.stream_content("s", "/f") as content:
            assert b"".join(content.iter_bytes()) == b"hello"
    assert len(seen) == 2
