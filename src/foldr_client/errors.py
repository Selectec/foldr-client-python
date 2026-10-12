"""Errors deliberately omit tokens, signed URLs and response bodies from messages."""

from typing import Any

import httpx


class FoldrClientError(Exception):
    """Base for API, OAuth, protocol and transport errors."""


class ProtocolError(FoldrClientError):
    """An unexpected response or unsafe redirect; no retry was attempted."""

    def __init__(self, message: str, *, outcome_uncertain: bool = False):
        self.outcome_uncertain = outcome_uncertain
        super().__init__(message)


class TransportError(FoldrClientError):
    """A network failure. A token request may have consumed its single-use input."""

    def __init__(self, operation: str, *, outcome_uncertain: bool = False):
        self.operation = operation
        self.outcome_uncertain = outcome_uncertain
        super().__init__(f"{operation}: transport failed; outcome_uncertain={outcome_uncertain}")


class StreamError(FoldrClientError):
    """Content was interrupted; bytes already delivered must not be replayed blindly."""

    def __init__(self, bytes_received: int):
        self.bytes_received = bytes_received
        super().__init__(f"Content stream interrupted after {bytes_received} bytes")


class FoldrError(FoldrClientError):
    """HTTP failure, retaining Foldr's full problem JSON and response headers.

    ``body`` is capped at 64 KiB; ``body_truncated`` says when it was clipped.
    Non-JSON failures (including HTML 429s) have ``problem=None``.
    """

    def __init__(
        self,
        status_code: int,
        headers: httpx.Headers,
        problem: dict[str, Any] | None,
        body: bytes = b"",
        *,
        body_truncated: bool = False,
    ):
        self.status_code = status_code
        self.headers = headers
        self.problem = problem
        self.body = body
        self.body_truncated = body_truncated
        self.type = problem.get("type") if problem else None
        self.code = problem.get("code") if problem else None
        self.detail = problem.get("detail") if problem else None
        self.instance = problem.get("instance") if problem else None
        self.retry_after = headers.get("retry-after")
        # Provider text may contain user data or signed URLs: do not format it here.
        super().__init__(f"Foldr HTTP {status_code}")


class OAuthError(FoldrError):
    """OAuth endpoint or callback failure; ``error`` is the provider's error code.

    Token endpoint failures are never retried. ``outcome_uncertain`` is true for
    server failures, where a code or refresh token may already have been consumed.
    """

    def __init__(self, *args: Any, outcome_uncertain: bool = False, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.error = self.problem.get("error") if self.problem else None
        self.outcome_uncertain = outcome_uncertain
