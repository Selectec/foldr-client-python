"""Public site endpoints use deterministic responses and never credentials."""

import json

import httpx
import pytest

from foldr_client import FoldrClient, FoldrError, OAuthError, ProtocolError, TransportError
from foldr_client.site import FoldrSite

SITE = "https://files.example.com"


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, data=b"{}", *, fail=False):
        self.data = data
        self.fail = fail
        self.closed = False

    def __iter__(self):
        yield self.data
        if self.fail:
            raise httpx.ReadError("interrupted")

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("method", "endpoint", "data"),
    [
        ("openapi", "/api/v1/openapi.json", {"openapi": "3.1.0", "paths": {}}),
        (
            "authorization_metadata",
            "/.well-known/oauth-authorization-server",
            {"issuer": SITE, "registration_endpoint": SITE + "/oauth/register"},
        ),
        (
            "protected_resource_metadata",
            "/.well-known/oauth-protected-resource/api/v1",
            {"resource": SITE + "/api/v1", "authorization_servers": [SITE]},
        ),
    ],
)
def test_discovery_retains_provider_metadata_status_and_headers(method, endpoint, data):
    calls = []
    body = TrackedStream(json.dumps(data).encode())

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == SITE + endpoint
        assert request.headers["accept"] == "application/json"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"ETag": '"version-1"'}, stream=body)

    with FoldrSite(SITE + "/", transport=httpx.MockTransport(handle)) as site:
        result = getattr(site, method)()
    assert result.data == data
    assert result.status_code == 200
    assert result.headers["etag"] == '"version-1"'
    assert body.closed
    assert len(calls) == 1


def test_openapi_conditional_request_preserves_304():
    def handle(request):
        assert request.headers["if-none-match"] == 'W/"known-version"'
        return httpx.Response(304, headers={"ETag": 'W/"known-version"', "Age": "12"})

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        result = site.openapi(if_none_match='W/"known-version"')
    assert result.status_code == 304
    assert result.data is None
    assert result.headers["age"] == "12"


@pytest.mark.parametrize(
    ("method", "kwargs", "endpoint"),
    [
        ("openapi", {}, "/api/v1/openapi.json"),
        ("authorization_metadata", {}, "/.well-known/oauth-authorization-server"),
        (
            "protected_resource_metadata",
            {},
            "/.well-known/oauth-protected-resource/api/v1",
        ),
        ("problem_type", {"slug": "not-found"}, "/api/v1/problems/not-found"),
    ],
)
def test_head_reads_only_status_and_headers(method, kwargs, endpoint):
    body = TrackedStream(b"not JSON")

    def handle(request):
        assert request.method == "HEAD"
        assert str(request.url) == SITE + endpoint
        return httpx.Response(200, headers={"Content-Length": "1000"}, stream=body)

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        result = getattr(site, method)(**kwargs, head=True)
    assert result.status_code == 200
    assert result.data is None
    assert result.headers["content-length"] == "1000"
    assert body.closed


def test_problem_description_requests_json_and_escapes_one_opaque_segment():
    data = {"type": "example", "description": "A provider explanation", "new_field": True}

    def handle(request):
        assert request.url.raw_path == b"/api/v1/problems/custom%2Fproblem%3Fname%3D%25"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json=data)

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        assert site.problem_type("custom/problem?name=%").data == data


@pytest.mark.parametrize("slug", ["", ".", ".."])
def test_problem_slug_cannot_replace_endpoint(slug):
    def handle(request):
        pytest.fail("Invalid slug must not issue HTTP")

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        with pytest.raises(ValueError, match="slug"):
            site.problem_type(slug)


def test_registration_posts_anonymous_json_and_retains_server_substitutions():
    metadata = {
        "client_name": "Reports",
        "redirect_uris": ["https://reports.example.com/callback"],
        "token_endpoint_auth_method": "client_secret_post",
        "scope": "foldr.read unknown-scope",
        "extension": {"future": True},
    }
    registered = {**metadata, "client_id": "issued-id", "client_secret": "issued-secret"}
    registered["scope"] = "foldr.read"
    calls = []
    body = TrackedStream(json.dumps(registered).encode())

    def handle(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == SITE + "/oauth/register"
        assert request.headers["content-type"] == "application/json"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert json.loads(request.content) == metadata
        return httpx.Response(201, headers={"Location": "/unfollowed"}, stream=body)

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        result = site.register(metadata)
    assert result.data == registered
    assert result.status_code == 201
    assert result.headers["location"] == "/unfollowed"
    assert metadata["scope"] == "foldr.read unknown-scope"
    assert body.closed
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 429, 503])
def test_registration_oauth_errors_are_preserved_and_not_retried(status):
    calls = []
    data = {"error": "invalid_client_metadata", "error_description": "Unusable name"}
    body = TrackedStream(json.dumps(data).encode())

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "30"}, stream=body)

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        with pytest.raises(OAuthError) as error:
            site.register({"client_name": "Reports"})
    assert error.value.problem == data
    assert error.value.status_code == status
    assert error.value.error == "invalid_client_metadata"
    assert error.value.retry_after == "30"
    assert error.value.outcome_uncertain is (status >= 500)
    assert body.closed
    assert len(calls) == 1


@pytest.mark.parametrize("method,uncertain", [("openapi", False), ("register", True)])
def test_transport_failures_never_retry(method, uncertain):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError("network unavailable", request=request)

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        with pytest.raises(TransportError) as error:
            getattr(site, method)(*([{}] if method == "register" else []))
    assert error.value.outcome_uncertain is uncertain
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["openapi", "register"])
def test_redirect_is_returned_as_error_without_following(method):
    calls = []
    body = TrackedStream(b"redirect")

    def handle(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"Location": "https://other.example.com/collect"}, stream=body
        )

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        expected = OAuthError if method == "register" else FoldrError
        with pytest.raises(expected) as error:
            getattr(site, method)(*([{}] if method == "register" else []))
    assert error.value.status_code == 302
    assert len(calls) == 1
    assert body.closed


def test_site_does_not_reuse_response_cookies_or_authenticated_client_credentials():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/v1/me":
            assert request.headers["authorization"] == "Bearer identity-secret"
        else:
            assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, json={}, headers={"Set-Cookie": "session=secret; Path=/"})

    with FoldrClient(
        SITE, access_token="identity-secret", transport=httpx.MockTransport(handle)
    ) as client:
        client.me()
        with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
            site.openapi()
            site.authorization_metadata()
    assert len(calls) == 3


@pytest.mark.parametrize("body", [b"not JSON", b'{"incomplete":'])
def test_malformed_discovery_closes_response(body):
    stream = TrackedStream(body)
    with FoldrSite(
        SITE, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as site:
        with pytest.raises(ProtocolError):
            site.openapi()
    assert stream.closed


def test_interrupted_discovery_closes_response():
    stream = TrackedStream(b'{"partial":', fail=True)
    with FoldrSite(
        SITE, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as site:
        with pytest.raises(TransportError):
            site.openapi()
    assert stream.closed


def test_public_problem_error_retains_structured_details():
    data = {"type": SITE + "/api/v1/problems/not-found", "code": "not-found", "extra": 1}
    with FoldrSite(
        SITE, transport=httpx.MockTransport(lambda request: httpx.Response(404, json=data))
    ) as site:
        with pytest.raises(FoldrError) as error:
            site.problem_type("future-type")
    assert error.value.problem == data


def test_close_releases_owned_transport():
    class Transport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True

    transport = Transport(lambda request: httpx.Response(200, json={}))
    with FoldrSite(SITE, transport=transport):
        assert not transport.closed
    assert transport.closed


@pytest.mark.parametrize("metadata", [[], "invalid", None, {"number": float("nan")}])
def test_registration_requires_serializable_json_object_before_http(metadata):
    def handle(request):
        pytest.fail("Invalid JSON metadata must not issue HTTP")

    with FoldrSite(SITE, transport=httpx.MockTransport(handle)) as site:
        with pytest.raises(ValueError):
            site.register(metadata)
