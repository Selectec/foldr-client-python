"""Minimal envelopes; resource dictionaries keep Foldr's canonical values."""

from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import ProtocolError, StreamError


@dataclass(frozen=True, repr=False)
class ApiResponse:
    """A closed HTTP response with parsed JSON and all response headers.

    ``data`` is None for HEAD, 204 and 304. Sensitive response data and signed
    URLs are deliberately excluded from repr. No operation is polled implicitly.
    """

    data: Any
    status_code: int
    headers: httpx.Headers

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def location(self) -> str | None:
        return self.headers.get("location")

    @property
    def retry_after(self) -> str | None:
        return self.headers.get("retry-after")


@dataclass(frozen=True)
class Page:
    """One provider page, including all fields in ``raw`` and HTTP ``headers``.

    ``truncated=None`` means Foldr did not supply that flag (as in search).
    No property infers completeness from a null cursor or an empty items list.
    """

    raw: dict[str, Any]
    headers: httpx.Headers = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.raw.get("items"), list) or "next_cursor" not in self.raw:
            raise ProtocolError("Page must include items and next_cursor")
        cursor = self.raw["next_cursor"]
        if cursor is not None and not isinstance(cursor, str):
            raise ProtocolError("Page cursor must be a string or null")

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.raw["items"]

    @property
    def next_cursor(self) -> str | None:
        return self.raw["next_cursor"]

    @property
    def truncated(self) -> bool | None:
        return self.raw.get("truncated")

    @property
    def not_searched(self) -> list[dict[str, Any]] | None:
        return self.raw.get("not_searched")

    @property
    def total_estimate(self) -> int | None:
        return self.raw.get("total_estimate")


class ContentStream:
    """Single-use byte stream. Owned by the ``stream_content`` context manager."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.status_code = response.status_code
        self.headers = httpx.Headers(response.headers)
        self.bytes_received = 0

    def iter_bytes(self, chunk_size: int = 65536):
        """Yield decoded content in bounded chunks; raise StreamError on interruption."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        try:
            for chunk in self._response.iter_bytes(chunk_size=chunk_size):
                self.bytes_received += len(chunk)
                yield chunk
        except httpx.HTTPError:
            raise StreamError(self.bytes_received) from None
