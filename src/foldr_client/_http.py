"""Small shared HTTP mechanics, with no retry or credential persistence."""

import json
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

from .errors import FoldrError, OAuthError, ProtocolError, TransportError
from .models import ApiResponse

UploadContent = bytes | BinaryIO | Iterable[bytes]


def iter_content(content: UploadContent, length: int) -> Iterator[bytes]:
    """Read at most length bytes from a caller-owned file; never close it.

    Byte strings and iterables must have exactly the declared size. File objects
    may have later data, allowing consecutive chunks of a direct upload.
    """
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ValueError("content_length must be a nonnegative integer")
    if isinstance(content, bytes):
        if len(content) != length:
            raise ValueError("Byte body length differs from content_length")
        source = (content,)
    elif hasattr(content, "read"):
        remaining = length
        while remaining:
            read_size = min(65536, remaining)
            chunk = content.read(read_size)
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > read_size:
                raise ProtocolError(
                    "Upload file ended before declared length", outcome_uncertain=True
                )
            remaining -= len(chunk)
            yield chunk
        return
    else:
        source = iter(content)
    received = 0
    for chunk in source:
        if not isinstance(chunk, bytes):
            raise ProtocolError("Upload iterator must yield bytes", outcome_uncertain=True)
        received += len(chunk)
        if received > length:
            raise ProtocolError("Upload body exceeds declared length", outcome_uncertain=True)
        for offset in range(0, len(chunk), 65536):
            yield chunk[offset : offset + 65536]
    if received != length:
        raise ProtocolError("Upload body ended before declared length", outcome_uncertain=True)


def site_url(site: str) -> str:
    parsed = urlsplit(site)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(
            "site must be an HTTPS origin, without credentials, path, query or fragment"
        )
    return site.rstrip("/")


def raise_response(response: httpx.Response, *, oauth: bool = False) -> None:
    body = bytearray()
    truncated = False
    # Even a misbehaving storage server's error must not buffer a whole file.
    # Cap wire bytes before decompression. Pre-buffered responses occur with
    # test transports; real responses are sent with stream=True.
    chunks = (
        (response.content,) if response.is_stream_consumed else response.iter_raw(chunk_size=8192)
    )
    for chunk in chunks:
        remaining = 65536 - len(body)
        body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    problem = None
    if not truncated:
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                problem = value
        except (ValueError, UnicodeError):
            pass
    cls = OAuthError if oauth else FoldrError
    extra = {"outcome_uncertain": response.status_code >= 500} if oauth else {}
    raise cls(
        response.status_code,
        httpx.Headers(response.headers),
        problem,
        bytes(body),
        body_truncated=truncated,
        **extra,
    )


class HTTP:
    def __init__(
        self,
        *,
        timeout: float | httpx.Timeout = 60.0,
        transport: httpx.BaseTransport | None = None,
        max_json_bytes: int = 8 * 1024 * 1024,
    ):
        if type(max_json_bytes) is not int or max_json_bytes <= 0:
            raise ValueError("max_json_bytes must be a positive integer")
        self.max_json_bytes = max_json_bytes
        # A private session per instance. Explicit Request objects never inherit
        # cookies, auth, URL credentials or headers from earlier responses.
        self.http = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        content: str | bytes | Iterable[bytes] | None = None,
        operation: str,
        uncertain: bool = False,
    ) -> httpx.Response:
        request = httpx.Request(
            method,
            url,
            params=params,
            headers=headers,
            content=content,
            extensions={"timeout": self.http.timeout.as_dict()},
        )
        try:
            return self.http.send(request, stream=True, auth=None, follow_redirects=False)
        except httpx.HTTPError:
            raise TransportError(operation, outcome_uncertain=uncertain) from None
        except OSError:
            raise TransportError(operation, outcome_uncertain=uncertain) from None

    def _json_value(self, response: httpx.Response, *, uncertain: bool) -> Any:
        # Reject compression before reading: iter_bytes/read would decompress a
        # small wire response into potentially unbounded memory. Content-Length
        # is not trusted; enforce the limit against the bytes actually received.
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise ProtocolError(
                "JSON response must honor Accept-Encoding: identity", outcome_uncertain=uncertain
            )
        body = bytearray()
        # Pre-buffered responses can be supplied by custom/test transports. The
        # transport owns any buffering before this method receives the response.
        chunks = (
            (response.content,)
            if response.is_stream_consumed
            else response.iter_raw(chunk_size=min(8192, self.max_json_bytes + 1))
        )
        for chunk in chunks:
            if len(chunk) > self.max_json_bytes - len(body):
                raise ProtocolError(
                    "JSON response exceeds max_json_bytes", outcome_uncertain=uncertain
                )
            body.extend(chunk)
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("Expected a JSON response", outcome_uncertain=uncertain) from None

    def read_response(
        self,
        response: httpx.Response,
        *,
        ok: tuple[int, ...] = (200,),
        oauth: bool = False,
        operation: str,
        uncertain: bool = False,
        head: bool = False,
    ) -> ApiResponse:
        try:
            if response.status_code not in ok:
                raise_response(response, oauth=oauth)
            if head or response.status_code in (204, 304):
                data = None
            else:
                data = self._json_value(response, uncertain=uncertain)
                if not isinstance(data, (dict, list)):
                    raise ProtocolError(
                        "Expected a JSON object or array", outcome_uncertain=uncertain
                    )
            return ApiResponse(
                data=data, status_code=response.status_code, headers=httpx.Headers(response.headers)
            )
        except httpx.HTTPError:
            raise TransportError(operation, outcome_uncertain=uncertain) from None
        finally:
            response.close()

    def read_json(
        self,
        response: httpx.Response,
        *,
        oauth: bool = False,
        operation: str,
        uncertain: bool = False,
    ) -> dict[str, Any]:
        try:
            if response.status_code != 200:
                raise_response(response, oauth=oauth)
            data = self._json_value(response, uncertain=uncertain)
            if not isinstance(data, dict):
                raise ProtocolError("Expected a JSON object", outcome_uncertain=uncertain)
            return data
        except httpx.HTTPError:
            raise TransportError(operation, outcome_uncertain=uncertain) from None
        finally:
            response.close()

    def close(self) -> None:
        self.http.close()
