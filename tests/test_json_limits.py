"""Successful JSON is bounded before parsing, including token/mutation outcomes."""

import gzip

import httpx
import pytest

from foldr_client import FoldrClient, FoldrOAuth, FoldrSite, ProtocolError

SITE = "https://files.example.com"
CASES = ("read", "envelope", "write", "oauth", "discovery", "registration")


class TrackedBody(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content
        self.delivered = 0
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.content), 4096):
            chunk = self.content[offset : offset + 4096]
            self.delivered += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def invoke(case, handler, **options):
    options["transport"] = httpx.MockTransport(handler)
    if case in ("read", "envelope", "write"):
        with FoldrClient(SITE, access_token="synthetic", **options) as client:
            if case == "read":
                return client.me()
            if case == "envelope":
                return client.list_fields().data
            return client.create_folder("share", "/new", idempotency_key="key").data
    if case == "oauth":
        with FoldrOAuth(SITE, client_id="synthetic", **options) as oauth:
            return oauth.refresh("synthetic-refresh")
    with FoldrSite(SITE, **options) as site:
        if case == "registration":
            return site.register({"client_name": "Synthetic"}).data
        return site.openapi().data


def response_status(case):
    return 201 if case in ("write", "registration") else 200


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("declared_length", [None, "2", "99999999"])
def test_success_limit_uses_actual_bytes_stops_early_and_closes(case, declared_length):
    body = TrackedBody(b'{"data":"' + b"x" * 1024 * 1024 + b'"}')
    calls = []

    def handle(request):
        calls.append(request)
        headers = {} if declared_length is None else {"Content-Length": declared_length}
        return httpx.Response(response_status(case), headers=headers, stream=body)

    with pytest.raises(ProtocolError) as caught:
        invoke(case, handle, max_json_bytes=32768)
    assert caught.value.outcome_uncertain is (case in ("write", "oauth", "registration"))
    assert 32768 < body.delivered <= 32768 + 8192
    assert body.closed
    assert len(calls) == 1


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_success_compression_is_rejected_without_consuming_body(case, encoding):
    body = TrackedBody(gzip.compress(b'{"data":"' + b"x" * 1024 * 1024 + b'"}'))

    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            response_status(case), headers={"Content-Encoding": encoding}, stream=body
        )

    with pytest.raises(ProtocolError) as caught:
        invoke(case, handle)
    assert caught.value.outcome_uncertain is (case in ("write", "oauth", "registration"))
    assert body.delivered == 0
    assert body.closed


@pytest.mark.parametrize("case", CASES)
def test_exact_limit_success_is_accepted(case):
    content = b'{"access_token":"a","token_type":"Bearer","refresh_token":"r"}'
    body = TrackedBody(content)

    def handle(request):
        return httpx.Response(response_status(case), stream=body)

    assert invoke(case, handle, max_json_bytes=len(content))["access_token"] == "a"
    assert body.closed


@pytest.mark.parametrize("case", ["read", "discovery", "oauth"])
def test_oauth_default_is_smaller_than_api_and_discovery(case):
    content = (
        b'{"access_token":"a","token_type":"Bearer","refresh_token":"r","extension":"'
        + b"x" * 70000
        + b'"}'
    )

    def handle(request):
        return httpx.Response(200, stream=TrackedBody(content))

    if case == "oauth":
        with pytest.raises(ProtocolError) as caught:
            invoke(case, handle)
        assert caught.value.outcome_uncertain
        assert invoke(case, handle, max_json_bytes=len(content))["access_token"] == "a"
    else:
        assert invoke(case, handle)["access_token"] == "a"


@pytest.mark.parametrize("case", CASES)
def test_prebuffered_custom_transport_success_still_obeys_limit(case):
    def handle(request):
        return httpx.Response(response_status(case), json={"secret": "x" * 100})

    with pytest.raises(ProtocolError):
        invoke(case, handle, max_json_bytes=32)


@pytest.mark.parametrize("case", ["read", "oauth", "discovery"])
@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_invalid_limit_is_rejected_before_network(case, limit):
    def handle(request):
        pytest.fail("Invalid configuration must not send a request")

    with pytest.raises(ValueError, match="max_json_bytes"):
        invoke(case, handle, max_json_bytes=limit)


@pytest.mark.parametrize("case", ["write", "registration"])
def test_malformed_mutation_success_remains_uncertain(case):
    body = TrackedBody(b"not JSON")
    with pytest.raises(ProtocolError) as caught:
        invoke(case, lambda _: httpx.Response(response_status(case), stream=body))
    assert caught.value.outcome_uncertain
    assert body.closed


def test_limit_does_not_cap_file_streams():
    with FoldrClient(
        SITE,
        access_token="synthetic",
        max_json_bytes=2,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"file bytes")),
    ) as client:
        with client.stream_content("share", "/file") as stream:
            assert b"".join(stream.iter_bytes()) == b"file bytes"


@pytest.mark.parametrize("character", [*(chr(code) for code in range(33)), "\x7f", "é"])
def test_access_token_rejects_controls_whitespace_and_non_ascii_without_echoing(character):
    token = "private" + character + "token"
    with pytest.raises(ValueError) as caught:
        FoldrClient(SITE, access_token=token)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("token", ["", None, b"secret", 123])
def test_access_token_requires_a_nonempty_string(token):
    with pytest.raises(ValueError):
        FoldrClient(SITE, access_token=token)
