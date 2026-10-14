"""Explicit Foldr OAuth protocol operations; credentials remain caller-owned."""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, parse_qs, quote_plus, urlencode, urlsplit

import httpx
from oauthlib.oauth2 import WebApplicationClient

from ._http import HTTP, raise_response, site_url
from .errors import OAuthError, ProtocolError, TransportError


def _callback_parts(url: str) -> SplitResult:
    """Parse without silently accepting userinfo or stripped URL controls."""
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Invalid callback URL")
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Invalid callback URL")
    # Accessing port validates its spelling and range; preserve its original text.
    _ = parsed.port
    return parsed


def _callback_target(parsed: SplitResult) -> tuple[str, str, str]:
    authority = parsed.netloc
    if not authority.startswith("["):
        host, separator, port = authority.partition(":")
        authority = host.lower() + separator + port
    return parsed.scheme, authority, parsed.path


@dataclass(frozen=True, repr=False)
class Authorization:
    """Caller-held, short-lived authorization context. Never log casually.

    The caller binds this to its session and allows it to be consumed only once.
    """

    url: str
    state: str
    code_verifier: str
    redirect_uri: str
    site: str
    client_id: str


class FoldrOAuth:
    """OAuth with none, client_secret_basic or client_secret_post authentication.

    No tokens are retained or installed in a FoldrClient. Use a context manager
    or close(). All protocol requests are made exactly once.
    """

    def __init__(
        self,
        site: str,
        *,
        client_id: str,
        client_secret: str | None = None,
        auth_method: str = "none",
        timeout: float | httpx.Timeout = 60.0,
        max_json_bytes: int = 64 * 1024,
        transport: httpx.BaseTransport | None = None,
    ):
        self.site = site_url(site)
        self.client_id = client_id
        if not client_id:
            raise ValueError("client_id must be nonempty")
        if auth_method not in ("none", "client_secret_basic", "client_secret_post"):
            raise ValueError("Unsupported token endpoint authentication method")
        if (auth_method == "none" and client_secret is not None) or (
            auth_method != "none" and not client_secret
        ):
            raise ValueError("client_secret must match the authentication method")
        self._client_secret = client_secret
        self.auth_method = auth_method
        self._connection = HTTP(timeout=timeout, transport=transport, max_json_bytes=max_json_bytes)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._connection.close()

    def authorize(self, redirect_uri: str, *, scopes: Sequence[str]) -> Authorization:
        """Generate an S256 PKCE URL and state; caller opens URL and retains context."""
        if isinstance(scopes, str):
            raise ValueError("scopes must be a sequence of scope strings")
        try:
            parsed = _callback_parts(redirect_uri)
        except ValueError:
            raise ValueError("Invalid redirect_uri") from None
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not (
                parsed.scheme == "https"
                or (
                    parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                )
            )
        ):
            raise ValueError("redirect_uri must be HTTPS or HTTP loopback, without a fragment")
        oauth = WebApplicationClient(self.client_id)
        verifier = oauth.create_code_verifier(64)
        challenge = oauth.create_code_challenge(verifier, code_challenge_method="S256")
        state = secrets.token_urlsafe(32)
        url = oauth.prepare_request_uri(
            self.site + "/oauth/authorize",
            redirect_uri=redirect_uri,
            scope=list(scopes),
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
            resource=self.site + "/api/v1",
        )
        return Authorization(url, state, verifier, redirect_uri, self.site, self.client_id)

    def exchange_callback(
        self,
        callback_url: str,
        authorization: Authorization,
    ) -> dict[str, Any]:
        """Validate callback target, state and issuer, then exchange code once.

        Caller binds authorization to its browser/session and prevents replay.
        This client stores no callback/session state.
        """
        if authorization.site != self.site or authorization.client_id != self.client_id:
            raise ProtocolError("Authorization context belongs to another site or client")
        try:
            actual = _callback_parts(callback_url)
            expected = _callback_parts(authorization.redirect_uri)
        except ValueError:
            raise ProtocolError("Malformed OAuth callback target") from None
        if actual.fragment or _callback_target(actual) != _callback_target(expected):
            raise ProtocolError("OAuth callback target does not match redirect_uri")
        query = parse_qs(actual.query, keep_blank_values=True)
        for key, values in parse_qs(expected.query, keep_blank_values=True).items():
            if query.get(key) != values:
                raise ProtocolError("OAuth callback lost registered query parameters")
        if len(query.get("state", [])) != 1 or not secrets.compare_digest(
            query["state"][0].encode(),
            authorization.state.encode(),
        ):
            raise ProtocolError("OAuth callback state does not match")
        if query.get("iss") != [self.site]:
            raise ProtocolError("OAuth callback issuer does not match")
        if "error" in query:
            if len(query["error"]) != 1 or "code" in query:
                raise ProtocolError("Malformed OAuth callback")
            raise OAuthError(400, httpx.Headers(), {"error": query["error"][0]})
        if len(query.get("code", [])) != 1 or not query["code"][0]:
            raise ProtocolError("OAuth callback must include exactly one nonempty code")
        oauth = WebApplicationClient(self.client_id)
        body = oauth.prepare_request_body(
            code=query["code"][0],
            redirect_uri=authorization.redirect_uri,
            code_verifier=authorization.code_verifier,
            include_client_id=self.auth_method != "client_secret_basic",
            resource=self.site + "/api/v1",
        )
        return self._tokens(body)

    def refresh(self, refresh_token: str, *, scopes: Sequence[str] | None = None) -> dict[str, Any]:
        """One refresh attempt. Caller serializes, persists and installs the result."""
        if not refresh_token:
            raise ValueError("refresh_token must be nonempty")
        if isinstance(scopes, str):
            raise ValueError("scopes must be a sequence of scope strings")
        oauth = WebApplicationClient(self.client_id)
        body = oauth.prepare_refresh_body(refresh_token=refresh_token, scope=scopes)
        return self._tokens(body, require_refresh=True)

    def _request(self, endpoint: str, body: str, *, uncertain: bool) -> httpx.Response:
        form = parse_qs(body, keep_blank_values=True)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self.auth_method == "client_secret_basic":
            # OAuth Basic percent-encodes ID/secret individually before joining.
            basic = httpx.BasicAuth(
                quote_plus(self.client_id), quote_plus(self._client_secret or "")
            )
            request = next(basic.auth_flow(httpx.Request("POST", self.site + endpoint)))
            headers["Authorization"] = request.headers["Authorization"]
        else:
            form["client_id"] = [self.client_id]
            if self.auth_method == "client_secret_post":
                form["client_secret"] = [self._client_secret or ""]
        return self._connection.send(
            "POST",
            self.site + endpoint,
            headers=headers,
            content=urlencode(form, doseq=True),
            operation="OAuth " + endpoint.rsplit("/", 1)[-1],
            uncertain=uncertain,
        )

    def _tokens(self, body: str, *, require_refresh: bool = False) -> dict[str, Any]:
        response = self._request("/oauth/token", body, uncertain=True)
        try:
            tokens = self._connection.read_json(
                response,
                oauth=True,
                operation="OAuth token",
                uncertain=True,
            )
        except ProtocolError:
            raise ProtocolError(
                "Malformed token response; do not replay the request",
                outcome_uncertain=True,
            ) from None
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise ProtocolError(
                "Token response lacks an access token; do not replay the request",
                outcome_uncertain=True,
            )
        if (
            not isinstance(tokens.get("token_type"), str)
            or tokens["token_type"].lower() != "bearer"
        ):
            raise ProtocolError(
                "Token response has unsupported token type; do not replay the request",
                outcome_uncertain=True,
            )
        if require_refresh and (
            not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]
        ):
            raise ProtocolError(
                "Refresh response lacks a replacement refresh token; do not replay the request",
                outcome_uncertain=True,
            )
        return tokens

    def revoke(self, token: str, *, token_type_hint: str | None = None) -> None:
        """Revoke access token or whole refresh connection; accept empty 200."""
        if not token:
            raise ValueError("token must be nonempty")
        if token_type_hint not in (None, "access_token", "refresh_token"):
            raise ValueError("token_type_hint must be access_token or refresh_token")
        form = {"token": token}
        if token_type_hint is not None:
            form["token_type_hint"] = token_type_hint
        response = self._request("/oauth/revoke", urlencode(form), uncertain=False)
        try:
            if response.status_code != 200:
                raise_response(response, oauth=True)
        except httpx.HTTPError:
            raise TransportError("OAuth revoke") from None
        finally:
            response.close()
