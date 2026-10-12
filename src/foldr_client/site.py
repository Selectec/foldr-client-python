"""Anonymous Foldr discovery, problem documentation and app registration."""

import json
from typing import Any
from urllib.parse import quote

import httpx

from ._http import HTTP, site_url
from .models import ApiResponse


class FoldrSite:
    """One site's public endpoints, with a private, credential-free HTTP session.

    Use a context manager or ``close()`` to release connections. The instance
    owns its optional HTTPX ``transport``. Requests never follow redirects,
    inherit cookies or retry. Registration returns the provider's complete
    response; the caller keeps the registration and any client secret.
    """

    def __init__(
        self,
        site: str,
        *,
        timeout: float | httpx.Timeout = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.site = site_url(site)
        self._connection = HTTP(timeout=timeout, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _get(
        self,
        endpoint: str,
        *,
        if_none_match: str | None = None,
        head: bool = False,
        ok: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        response = self._connection.send(
            "HEAD" if head else "GET",
            self.site + endpoint,
            headers=headers,
            operation="Site read",
        )
        return self._connection.read_response(response, ok=ok, operation="Site read", head=head)

    def openapi(self, *, if_none_match: str | None = None, head: bool = False) -> ApiResponse:
        """Fetch the public OpenAPI document, preserving ETag and 304 responses."""
        return self._get(
            "/api/v1/openapi.json", if_none_match=if_none_match, head=head, ok=(200, 304)
        )

    def authorization_metadata(self, *, head: bool = False) -> ApiResponse:
        """Fetch the site's OAuth authorization server metadata."""
        return self._get("/.well-known/oauth-authorization-server", head=head)

    def protected_resource_metadata(self, *, head: bool = False) -> ApiResponse:
        """Fetch the API v1 protected resource metadata."""
        return self._get("/.well-known/oauth-protected-resource/api/v1", head=head)

    def problem_type(self, slug: str, *, head: bool = False) -> ApiResponse:
        """Fetch a public problem description as JSON, using one opaque slug."""
        if not slug or slug in (".", ".."):
            raise ValueError("slug must be a nonempty opaque identifier")
        return self._get("/api/v1/problems/" + quote(slug, safe=""), head=head)

    def register(self, metadata: dict[str, Any]) -> ApiResponse:
        """Register an app once (RFC 7591), without retaining its credentials.

        Foldr validates and may substitute metadata; inspect the returned
        values. On an uncertain failure, reconcile before registering again.
        """
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        response = self._connection.send(
            "POST",
            self.site + "/oauth/register",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            },
            content=json.dumps(metadata, allow_nan=False),
            operation="OAuth registration",
            uncertain=True,
        )
        return self._connection.read_response(
            response,
            ok=(201,),
            oauth=True,
            operation="OAuth registration",
            uncertain=True,
        )
