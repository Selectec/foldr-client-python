"""Send direct-upload bytes using only the storage credentials in a Foldr session."""

import re
from collections.abc import Iterable
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

from ._http import HTTP, iter_content, raise_response
from .errors import ProtocolError, TransportError
from .models import ApiResponse


class DirectUpload:
    """A separate, credential-isolated connection for a direct upload session.

    The session comes from ``FoldrClient.create_upload_session``. Its signed URL
    and headers are credentials: retain them privately. This helper never takes
    a Foldr token, follows redirects, retries, or completes the Foldr session.
    Close with a context manager or ``close()``. Caller-owned streams stay open.
    """

    def __init__(
        self,
        session: dict[str, Any],
        *,
        timeout: float | httpx.Timeout = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        url = session.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Upload session URL must be HTTPS without URL credentials or fragment")
        method = session.get("method")
        if not isinstance(method, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method):
            raise ValueError("Upload session must supply an HTTP method")
        headers = session.get("headers")
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("Upload session headers must be a mapping of strings")
        try:
            self._headers = httpx.Headers(headers)
        except (ValueError, UnicodeError):
            raise ValueError("Upload session contains invalid headers") from None
        size, chunk_size = session.get("size"), session.get("chunk_size")
        if type(size) is not int or size < 0:
            raise ValueError("Upload session size must be a nonnegative integer")
        if chunk_size is not None and (type(chunk_size) is not int or chunk_size <= 0):
            raise ValueError("Upload session chunk_size must be a positive integer or null")
        self._url = url
        self._method = method
        self._size = size
        self._chunk_size = chunk_size
        self._connection = HTTP(timeout=timeout, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._connection.close()

    def send(
        self,
        content: bytes | BinaryIO | Iterable[bytes],
        *,
        content_length: int,
        content_range: str | None = None,
    ) -> ApiResponse:
        """Send one whole file or one session-sized piece, exactly once.

        For chunked sessions use ``bytes start-end/total``. Pieces start on a
        ``chunk_size`` boundary and contain exactly that many bytes except the
        final piece. Intermediate responses are 202; the final response is 200
        or 201. Storage response bodies are discarded; status and headers remain.
        Call Foldr's complete-upload operation after storage accepts all bytes.
        """
        if type(content_length) is not int or content_length < 0:
            raise ValueError("content_length must be a nonnegative integer")
        if isinstance(content, bytes) and len(content) != content_length:
            raise ValueError("Byte body length differs from content_length")
        final_piece = True
        if self._chunk_size is None:
            if content_range is not None or content_length != self._size:
                raise ValueError("Whole-file upload must match session size and omit Content-Range")
        else:
            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range or "")
            if match is None:
                raise ValueError("Chunked upload requires Content-Range: bytes start-end/total")
            start, end, total = map(int, match.groups())
            if (
                total != self._size
                or start > end
                or end >= total
                or start % self._chunk_size != 0
                or end - start + 1 != content_length
                or content_length != min(self._chunk_size, total - start)
            ):
                raise ValueError(
                    "Content-Range and length must describe a session-sized upload piece"
                )
            final_piece = end == total - 1
        headers = httpx.Headers(self._headers)
        if "content-length" in headers and headers["content-length"] != str(content_length):
            raise ValueError("Content-Length conflicts with the session headers")
        if "content-range" in headers and headers["content-range"] != content_range:
            raise ValueError("Content-Range conflicts with the session headers")
        if "transfer-encoding" in headers:
            raise ValueError("Upload session cannot combine Transfer-Encoding with Content-Length")
        headers["Content-Length"] = str(content_length)
        if content_range is not None:
            headers["Content-Range"] = content_range
        response = self._connection.send(
            self._method,
            self._url,
            headers=dict(headers),
            content=iter_content(content, content_length),
            operation="Direct upload",
            uncertain=True,
        )
        try:
            expected = (200, 201) if final_piece else (202,)
            if response.status_code not in expected:
                if 200 <= response.status_code < 300:
                    raise ProtocolError(
                        "Storage returned an unexpected upload status", outcome_uncertain=True
                    )
                raise_response(response)
            return ApiResponse(
                data=None,
                status_code=response.status_code,
                headers=httpx.Headers(response.headers),
            )
        except httpx.HTTPError:
            raise TransportError("Direct upload", outcome_uncertain=True) from None
        finally:
            response.close()
