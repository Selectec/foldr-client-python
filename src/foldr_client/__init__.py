"""Thin synchronous client for the Foldr API v1."""

from .client import FoldrClient
from .errors import (
    FoldrClientError,
    FoldrError,
    OAuthError,
    ProtocolError,
    StreamError,
    TransportError,
)
from .models import ApiResponse, ContentStream, Page
from .oauth import Authorization, FoldrOAuth
from .site import FoldrSite
from .uploads import DirectUpload

__all__ = [
    "ApiResponse",
    "Authorization",
    "ContentStream",
    "DirectUpload",
    "FoldrClient",
    "FoldrClientError",
    "FoldrError",
    "FoldrOAuth",
    "FoldrSite",
    "OAuthError",
    "Page",
    "ProtocolError",
    "StreamError",
    "TransportError",
]
