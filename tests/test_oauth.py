"""OAuth protocol checks with deterministic HTTP fakes and no live credentials."""

import base64
import hashlib
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

import httpx
import pytest
from oauthlib.oauth2 import WebApplicationClient

from foldr_client import FoldrClient, FoldrOAuth, OAuthError, ProtocolError, TransportError

SITE = "https://files.example.com"
CALLBACK = "https://app.example.com/callback?session=caller-owned"
TOKENS = {
    "access_token": "fdr_at_new-secret",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "fdr_rt_new-secret",
    "scope": "foldr.read",
    "provider_extension": {"preserved": True},
}


@pytest.fixture(autouse=True)
def deterministic_authorization(monkeypatch):
    monkeypatch.setattr(WebApplicationClient, "create_code_verifier", lambda self, length: "a" * 64)
    monkeypatch.setattr("foldr_client.oauth.secrets.token_urlsafe", lambda length: "fixed-state")


def callback(authorization, **updates):
    query = {
        "session": "caller-owned",
        "state": authorization.state,
        "iss": SITE,
        "code": "single-use-code",
    }
    query.update(updates)
    return CALLBACK.split("?", 1)[0] + "?" + urlencode(query, doseq=True)


def form(request):
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    return parse_qs(request.content.decode(), keep_blank_values=True)


def test_authorization_url_contains_s256_state_and_site_resource():
    with FoldrOAuth(SITE + "/", client_id="public-id") as oauth:
        authorization = oauth.authorize(
            CALLBACK, scopes=["foldr.read", "offline_access", "profile"]
        )
    parsed = urlsplit(authorization.url)
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == SITE + "/oauth/authorize"
    query = parse_qs(parsed.query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"a" * 64).digest()).rstrip(b"=").decode()
    assert query == {
        "response_type": ["code"],
        "client_id": ["public-id"],
        "redirect_uri": [CALLBACK],
        "scope": ["foldr.read offline_access profile"],
        "state": [authorization.state],
        "code_challenge": [challenge],
        "code_challenge_method": ["S256"],
        "resource": [SITE + "/api/v1"],
    }
    assert 43 <= len(authorization.code_verifier) <= 128
    assert authorization.state and authorization.code_verifier not in repr(authorization)


@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
def test_code_exchange_uses_exactly_registered_authentication_method(auth_method):
    requests = []
    client_id, secret = "client:id +é", "secret: +é"

    def handle(request):
        requests.append(request)
        assert str(request.url) == SITE + "/oauth/token"
        values = form(request)
        assert values["grant_type"] == ["authorization_code"]
        assert values["code"] == ["single-use-code"]
        assert values["redirect_uri"] == [CALLBACK]
        assert values["code_verifier"] == ["a" * 64]
        assert values["resource"] == [SITE + "/api/v1"]
        if auth_method == "client_secret_basic":
            basic = base64.b64decode(request.headers["authorization"].split()[1]).decode()
            assert basic == quote_plus(client_id) + ":" + quote_plus(secret)
            assert "client_id" not in values and "client_secret" not in values
        else:
            assert "authorization" not in request.headers
            assert values["client_id"] == [client_id]
            if auth_method == "client_secret_post":
                assert values["client_secret"] == [secret]
            else:
                assert "client_secret" not in values
        return httpx.Response(200, json=TOKENS)

    with FoldrOAuth(
        SITE,
        client_id=client_id,
        auth_method=auth_method,
        client_secret=secret if auth_method != "none" else None,
        transport=httpx.MockTransport(handle),
    ) as oauth:
        authorization = oauth.authorize(CALLBACK, scopes=["foldr.read", "offline_access"])
        assert oauth.exchange_callback(callback(authorization), authorization) == TOKENS
    assert len(requests) == 1


@pytest.mark.parametrize("port", ["", ":443", ":0443"])
def test_callback_hostname_case_preserves_original_registered_uri_in_exchange(port):
    registered = "https://App.Example.COM" + port + "/Callback?session=caller-owned"
    requests = []

    def handle(request):
        requests.append(request)
        assert form(request)["redirect_uri"] == [registered]
        return httpx.Response(200, json=TOKENS)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(registered, scopes=["foldr.read"])
        query = urlsplit(callback(authorization)).query
        actual = "https://APP.example.com" + port + "/Callback?" + query
        assert oauth.exchange_callback(actual, authorization) == TOKENS
    assert len(requests) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"state": "wrong-secret"},
        {"state": ""},
        {"state": ["fixed-state", "fixed-state"]},
        {"iss": "https://other.example.com"},
        {"iss": "https://FILES.example.com"},
        {"iss": ""},
        {"iss": [SITE, SITE]},
        {"code": ""},
        {"code": ["one", "two"]},
        {"session": "wrong"},
        {"session": ["caller-owned", "caller-owned"]},
        {"error": ["access_denied", "access_denied"]},
        {"error": "access_denied"},
    ],
)
def test_bad_callback_query_rejected_before_http(updates):
    def handle(request):
        pytest.fail("Invalid callback must not exchange a code")

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(CALLBACK, scopes=["foldr.read"])
        with pytest.raises(ProtocolError):
            oauth.exchange_callback(
                callback(authorization, **updates).replace("app.example.com", "APP.EXAMPLE.COM"),
                authorization,
            )


@pytest.mark.parametrize("missing", ["state", "iss", "code"])
def test_missing_callback_parameter_rejected_before_http(missing):
    def handle(request):
        pytest.fail("Invalid callback must not make an HTTP request")

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(CALLBACK, scopes=["foldr.read"])
        parsed = urlsplit(callback(authorization))
        values = parse_qs(parsed.query)
        del values[missing]
        url = parsed._replace(query=urlencode(values, doseq=True)).geturl()
        with pytest.raises(ProtocolError):
            oauth.exchange_callback(url, authorization)


@pytest.mark.parametrize(
    "target",
    [
        "http://app.example.com/callback",
        "https://evil.example.com/callback",
        "https://app.example.com/other",
        "https://app.example.com:444/callback",
        "https://APP.EXAMPLE.COM:443/callback",
        "https://APP.EXAMPLE.COM:/callback",
        "https://APP.EXAMPLE.COM/Callback",
        "https://APP.EXAMPLE.COM/call%62ack",
        "https://APP.EXAMPLE.COM/callback/",
        "https://secret@APP.EXAMPLE.COM/callback",
        "https://user:secret@APP.EXAMPLE.COM/callback",
        "https://APP.EXAMPLE.COM:secret/callback",
        "https://APP.EXAMPLE.COM:65536/callback",
        "https://[secret/callback",
        "https://APP.EXAMPLE.COM/call\nback",
        " https://APP.EXAMPLE.COM/callback",
    ],
)
def test_callback_target_checked_before_http(target):
    def handle(request):
        pytest.fail("Wrong callback target must not exchange a code")

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(CALLBACK, scopes=["foldr.read"])
        query = urlsplit(callback(authorization)).query
        with pytest.raises(ProtocolError) as caught:
            oauth.exchange_callback(target + "?" + query, authorization)
        assert "secret" not in str(caught.value)


def test_callback_port_spelling_remains_exact():
    def handle(request):
        pytest.fail("Changed port spelling must not exchange a code")

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(
            "https://app.example.com:0443/callback?session=caller-owned", scopes=["foldr.read"]
        )
        query = urlsplit(callback(authorization)).query
        with pytest.raises(ProtocolError):
            oauth.exchange_callback("https://APP.EXAMPLE.COM:443/callback?" + query, authorization)


def test_callback_fragment_and_foreign_authorization_context_are_rejected():
    def handle(request):
        pytest.fail("Foreign authorization context must not be exchanged")

    with FoldrOAuth(SITE, client_id="first", transport=httpx.MockTransport(handle)) as first:
        authorization = first.authorize(CALLBACK, scopes=["foldr.read"])
        with pytest.raises(ProtocolError):
            first.exchange_callback(callback(authorization) + "#fragment", authorization)
        for site, client_id in [(SITE, "second"), ("https://other.example.com", "first")]:
            with FoldrOAuth(
                site, client_id=client_id, transport=httpx.MockTransport(handle)
            ) as other:
                with pytest.raises(ProtocolError):
                    other.exchange_callback(callback(authorization), authorization)


def test_oauth_denial_is_preserved_without_http_or_secret_in_message():
    def handle(request):
        pytest.fail("An authorization denial must not exchange a code")

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        authorization = oauth.authorize(CALLBACK, scopes=["foldr.read"])
        url = callback(authorization, error="access_denied", code=[])
        with pytest.raises(OAuthError) as caught:
            oauth.exchange_callback(url, authorization)
    assert caught.value.error == "access_denied"
    assert authorization.state not in str(caught.value)
    assert "access_denied" not in str(caught.value)


def test_refresh_returns_unmodified_grant_without_installing_credentials():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/oauth/token":
            values = form(request)
            assert values == {
                "grant_type": ["refresh_token"],
                "refresh_token": ["fdr_rt_old-secret"],
                "scope": ["foldr.read"],
                "client_id": ["id"],
            }
            return httpx.Response(200, json=TOKENS)
        assert request.headers["authorization"] == "Bearer fdr_at_original"
        return httpx.Response(200, json={"name": "Original identity"})

    with (
        FoldrClient(
            SITE,
            access_token="fdr_at_original",
            transport=httpx.MockTransport(handle),
        ) as client,
        FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth,
    ):
        caller_tokens = {"refresh_token": "fdr_rt_old-secret"}
        refreshed = oauth.refresh(caller_tokens["refresh_token"], scopes=["foldr.read"])
        assert refreshed == TOKENS
        assert caller_tokens == {"refresh_token": "fdr_rt_old-secret"}
        assert client.me() == {"name": "Original identity"}
    assert len(requests) == 2


@pytest.mark.parametrize(
    "status,error",
    [
        (400, "invalid_grant"),
        (401, "invalid_client"),
        (429, "rate_limited"),
        (500, "server_error"),
    ],
)
def test_refresh_errors_preserve_provider_fields_and_are_never_retried(status, error):
    calls = []
    problem = {"error": error, "provider_extension": "fdr_rt_private-value"}

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=problem, headers={"Retry-After": "3"})

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(OAuthError) as caught:
            oauth.refresh("fdr_rt_old-secret")
    assert len(calls) == 1
    assert caught.value.status_code == status
    assert caught.value.error == error
    assert caught.value.problem == problem
    assert caught.value.retry_after == "3"
    assert caught.value.outcome_uncertain is (status >= 500)
    assert "fdr_rt_private-value" not in str(caught.value)
    assert "fdr_rt_old-secret" not in str(caught.value)


class InterruptedBody(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield b'{"access_token":"fdr_at_private'
        raise httpx.ReadError("response interrupted at https://signed.example/?secret=private")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("max_json_bytes", [None, 128])
def test_token_response_limit_closes_body_and_preserves_uncertain_outcome(max_json_bytes):
    calls = []
    limit = 64 * 1024 if max_json_bytes is None else max_json_bytes

    class OversizedBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"access_token":"' + b"x" * limit + b'","token_type":"Bearer"}'

        def close(self):
            self.closed = True

    body = OversizedBody()

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    options = {} if max_json_bytes is None else {"max_json_bytes": max_json_bytes}
    with FoldrOAuth(
        SITE, client_id="id", transport=httpx.MockTransport(handle), **options
    ) as oauth:
        with pytest.raises(ProtocolError) as caught:
            oauth.refresh("refresh-secret")
    assert caught.value.outcome_uncertain is True
    assert "refresh-secret" not in str(caught.value)
    assert len(calls) == 1
    assert body.closed


@pytest.mark.parametrize("failure", ["timeout", "connect", "read"])
def test_refresh_transport_failures_are_uncertain_and_never_retried(failure):
    calls = []
    body = InterruptedBody()

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("fdr_rt_private timeout", request=request)
        if failure == "connect":
            raise httpx.ConnectError("fdr_rt_private connect", request=request)
        return httpx.Response(200, stream=body)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(TransportError) as caught:
            oauth.refresh("fdr_rt_old-secret")
    assert len(calls) == 1
    assert caught.value.outcome_uncertain is True
    assert "private" not in str(caught.value)
    assert "signed.example" not in str(caught.value)
    if failure == "read":
        assert body.closed


@pytest.mark.parametrize(
    "tokens",
    [
        {},
        {"access_token": "x"},
        {"access_token": "", "token_type": "Bearer"},
        {"access_token": "x", "token_type": "unsupported-secret"},
    ],
)
def test_malformed_success_is_not_retried_or_exposed(tokens):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=tokens)

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(ProtocolError) as caught:
            oauth.refresh("refresh-secret")
    assert len(calls) == 1
    assert "unsupported-secret" not in str(caught.value)
    assert "refresh-secret" not in str(caught.value)


@pytest.mark.parametrize("hint", [None, "access_token", "refresh_token"])
@pytest.mark.parametrize("auth_method", ["none", "client_secret_basic", "client_secret_post"])
def test_revocation_accepts_empty_200_with_documented_authentication(hint, auth_method):
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == SITE + "/oauth/revoke"
        values = form(request)
        assert values["token"] == ["fdr_rt_revoke-secret"]
        assert values.get("token_type_hint") == ([hint] if hint else None)
        if auth_method == "client_secret_basic":
            assert (
                request.headers["authorization"]
                == "Basic " + base64.b64encode(b"id:secret").decode()
            )
            assert "client_id" not in values and "client_secret" not in values
        else:
            assert "authorization" not in request.headers
            assert values["client_id"] == ["id"]
            assert values.get("client_secret") == (
                ["secret"] if auth_method.endswith("post") else None
            )
        return httpx.Response(200, content=b"")

    with FoldrOAuth(
        SITE,
        client_id="id",
        auth_method=auth_method,
        client_secret="secret" if auth_method != "none" else None,
        transport=httpx.MockTransport(handle),
    ) as oauth:
        assert oauth.revoke("fdr_rt_revoke-secret", token_type_hint=hint) is None
    assert len(calls) == 1


def test_revocation_error_is_preserved_and_redacted():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "invalid_client", "detail": "client-secret"})

    with FoldrOAuth(SITE, client_id="id", transport=httpx.MockTransport(handle)) as oauth:
        with pytest.raises(OAuthError) as caught:
            oauth.revoke("token-secret")
    assert len(calls) == 1
    assert caught.value.error == "invalid_client"
    assert "client-secret" not in str(caught.value)
    assert "token-secret" not in str(caught.value)


@pytest.mark.parametrize("operation", ["exchange", "refresh", "revoke"])
def test_oauth_never_follows_redirects(operation):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/?signed-secret"})

    with FoldrOAuth(
        SITE,
        client_id="id",
        auth_method="client_secret_post",
        client_secret="secret",
        transport=httpx.MockTransport(handle),
    ) as oauth:
        with pytest.raises(OAuthError) as caught:
            if operation == "exchange":
                authorization = oauth.authorize(CALLBACK, scopes=["foldr.read"])
                oauth.exchange_callback(callback(authorization), authorization)
            elif operation == "refresh":
                oauth.refresh("refresh-secret")
            else:
                oauth.revoke("token-secret")
    assert len(calls) == 1
    assert calls[0].url.host == "files.example.com"
    assert caught.value.status_code == 302
    assert "signed-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "redirect",
    [
        "http://app.example.com/callback",
        "https://user:secret@app.example.com/callback",
        "https://app.example.com/callback#fragment",
        "javascript:alert(1)",
        "https://app.example.com:secret/callback",
        "https://app.example.com:65536/callback",
        "https://[secret/callback",
        "https://app.example.com/call\nback",
    ],
)
def test_authorization_rejects_unsafe_redirect_uri(redirect):
    with FoldrOAuth(SITE, client_id="id") as oauth:
        with pytest.raises(ValueError):
            oauth.authorize(redirect, scopes=["foldr.read"])


@pytest.mark.parametrize(
    "redirect",
    [
        "http://localhost:12345/callback",
        "http://127.0.0.1:12345/callback",
        "http://[::1]:12345/callback",
    ],
)
def test_authorization_accepts_registered_loopback_redirects(redirect):
    with FoldrOAuth(SITE, client_id="id") as oauth:
        authorization = oauth.authorize(redirect, scopes=["foldr.read"])
    assert parse_qs(urlsplit(authorization.url).query)["redirect_uri"] == [redirect]
