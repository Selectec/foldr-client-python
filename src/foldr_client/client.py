"""Synchronous operations specific to Foldr API v1."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ._destinations import check_destination
from ._http import HTTP, UploadContent, iter_content, raise_response, site_url
from .errors import ProtocolError, TransportError
from .models import ApiResponse, ContentStream, Page


class FoldrClient:
    """One Foldr site and identity. No automatic refresh, retries or token storage.

    Close with a context manager or ``close()``. ``transport`` is an optional
    HTTPX transport for testing/custom TLS. The client owns its transport.
    """

    def __init__(
        self,
        site: str,
        *,
        access_token: str,
        timeout: float | httpx.Timeout = 60.0,
        transport: httpx.BaseTransport | None = None,
        max_json_bytes: int = 8 * 1024 * 1024,
        destination_policy: Callable[[httpx.URL], bool] | None = None,
    ):
        self.site = site_url(site)
        self.api_url = self.site + "/api/v1"
        if (
            not isinstance(access_token, str)
            or not access_token
            or any(not 33 <= ord(c) <= 126 for c in access_token)
        ):
            raise ValueError("access_token must be nonempty visible ASCII without whitespace")
        self._access_token = access_token
        self._destination_policy = destination_policy
        self._connection = HTTP(timeout=timeout, transport=transport, max_json_bytes=max_json_bytes)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _share(share_id: str) -> str:
        # Encode as one segment, preventing accidental endpoint/path traversal.
        if not share_id or share_id in (".", ".."):
            raise ValueError("share_id must be a nonempty opaque identifier")
        return "/shares/" + quote(share_id, safe="")

    def _get(self, endpoint: str, **params: Any) -> tuple[dict[str, Any], httpx.Headers]:
        response = self._connection.send(
            "GET",
            self.api_url + endpoint,
            headers={
                "Authorization": "Bearer " + self._access_token,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            params={k: v for k, v in params.items() if v is not None},
            operation="API read",
        )
        headers = httpx.Headers(response.headers)
        return self._connection.read_json(response, operation="API read"), headers

    def me(self) -> dict[str, Any]:
        return self._get("/me")[0]

    def get_item(self, share_id: str, path: str) -> dict[str, Any]:
        return self._get(self._share(share_id) + "/item", path=path)[0]

    @staticmethod
    def _identifier(value: str) -> str:
        if not value or value in (".", ".."):
            raise ValueError("Identifier must be a nonempty opaque string")
        return quote(value, safe="")

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: Any = None,
        ok: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        request_headers = {
            "Authorization": "Bearer " + self._access_token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request_headers.update(headers or {})
        uncertain = method not in ("GET", "HEAD")
        response = self._connection.send(
            method,
            self.api_url + endpoint,
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers=request_headers,
            content=content,
            operation="Foldr " + method,
            uncertain=uncertain,
        )
        return self._connection.read_response(
            response,
            ok=ok,
            operation="Foldr " + method,
            uncertain=uncertain,
            head=method == "HEAD",
        )

    def get_share(self, share_id: str) -> dict[str, Any]:
        return self._request("GET", self._share(share_id)).data

    def get_item_response(
        self,
        share_id: str,
        path: str,
        *,
        if_none_match: str | None = None,
    ) -> ApiResponse:
        """Metadata with HTTP headers, including a conditional 304 with data=None."""
        return self._request(
            "GET",
            self._share(share_id) + "/item",
            params={"path": path},
            headers={"If-None-Match": if_none_match} if if_none_match is not None else {},
            ok=(200, 304),
        )

    def get_download_url(self, share_id: str, path: str) -> ApiResponse:
        """Return a signed URL and expiry; keep data and headers out of logs."""
        return self._request("GET", self._share(share_id) + "/download-url", params={"path": path})

    def list_fields(self) -> ApiResponse:
        return self._request("GET", "/fields")

    def get_field_values(
        self,
        share_id: str,
        path: str,
        *,
        if_none_match: str | None = None,
    ) -> ApiResponse:
        return self._request(
            "GET",
            self._share(share_id) + "/field-values",
            params={"path": path},
            headers={"If-None-Match": if_none_match} if if_none_match is not None else {},
            ok=(200, 304),
        )

    def list_versions(self, share_id: str, path: str) -> Page:
        response = self._request("GET", self._share(share_id) + "/versions", params={"path": path})
        return Page(response.data, response.headers)

    @staticmethod
    def _write_headers(
        *,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        exact_etag: bool = False,
        respond_async: bool = False,
    ) -> dict[str, str]:
        headers = {}
        if idempotency_key is not None:
            if not 1 <= len(idempotency_key) <= 255 or any(
                not 33 <= ord(c) <= 126 for c in idempotency_key
            ):
                raise ValueError("idempotency_key must be 1-255 visible ASCII characters")
            headers["Idempotency-Key"] = idempotency_key
        if if_match is not None:
            if not if_match or any(c in if_match for c in "\r\n"):
                raise ValueError("if_match must be a nonempty ETag without newlines")
            if exact_etag and if_match.strip() == "*":
                raise ValueError("This operation requires a version ETag, not '*'")
            headers["If-Match"] = if_match
        if respond_async:
            headers["Prefer"] = "respond-async"
        return headers

    @staticmethod
    def _body(**values: Any) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value is not None}

    def create_folder(
        self,
        share_id: str,
        path: str,
        *,
        idempotency_key: str,
        on_conflict: str | None = None,
    ) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/folders",
            body=self._body(path=path, on_conflict=on_conflict),
            headers=self._write_headers(idempotency_key=idempotency_key),
            ok=(201,),
        )

    def _upload(
        self,
        share_id: str,
        path: str,
        content: UploadContent,
        *,
        content_length: int,
        overwrite: bool,
        headers: dict[str, str],
        on_conflict: str | None = None,
    ) -> ApiResponse:
        if (
            isinstance(content_length, bool)
            or not isinstance(content_length, int)
            or content_length < 0
        ):
            raise ValueError("content_length must be a nonnegative integer")
        if isinstance(content, bytes) and len(content) != content_length:
            raise ValueError("Byte body length differs from content_length")
        headers = dict(headers)
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(content_length)
        if not overwrite:
            headers["If-None-Match"] = "*"
        return self._request(
            "PUT",
            self._share(share_id) + ("/content" if overwrite else "/files"),
            params=self._body(path=path, on_conflict=on_conflict),
            headers=headers,
            content=iter_content(content, content_length),
            ok=(200,) if overwrite else (201,),
        )

    def upload_file(
        self,
        share_id: str,
        path: str,
        content: UploadContent,
        *,
        content_length: int,
        idempotency_key: str,
        on_conflict: str | None = None,
    ) -> ApiResponse:
        """Stream exactly content_length bytes. Caller owns the body and retry key."""
        return self._upload(
            share_id,
            path,
            content,
            content_length=content_length,
            overwrite=False,
            headers=self._write_headers(idempotency_key=idempotency_key),
            on_conflict=on_conflict,
        )

    def overwrite_file(
        self,
        share_id: str,
        path: str,
        content: UploadContent,
        *,
        content_length: int,
        if_match: str,
    ) -> ApiResponse:
        return self._upload(
            share_id,
            path,
            content,
            content_length=content_length,
            overwrite=True,
            headers=self._write_headers(if_match=if_match, exact_etag=True),
        )

    def rename_item(self, share_id: str, path: str, new_name: str, *, if_match: str) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/rename",
            body={"path": path, "new_name": new_name},
            headers=self._write_headers(if_match=if_match),
        )

    def copy_item(
        self,
        share_id: str,
        path: str,
        *,
        to_share: str,
        to_path: str,
        idempotency_key: str,
        on_conflict: str | None = None,
        respond_async: bool = False,
    ) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/copy",
            body=self._body(path=path, to_share=to_share, to_path=to_path, on_conflict=on_conflict),
            headers=self._write_headers(
                idempotency_key=idempotency_key, respond_async=respond_async
            ),
            ok=(201, 202),
        )

    def move_item(
        self,
        share_id: str,
        path: str,
        *,
        to_path: str,
        if_match: str,
        idempotency_key: str,
        to_share: str | None = None,
        on_conflict: str | None = None,
        respond_async: bool = False,
    ) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/move",
            body=self._body(path=path, to_path=to_path, to_share=to_share, on_conflict=on_conflict),
            headers=self._write_headers(
                idempotency_key=idempotency_key,
                if_match=if_match,
                exact_etag=True,
                respond_async=respond_async,
            ),
            ok=(200, 202),
        )

    def delete_item(
        self,
        share_id: str,
        path: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> ApiResponse:
        return self._request(
            "DELETE",
            self._share(share_id) + "/items",
            params={"path": path},
            headers=self._write_headers(
                idempotency_key=idempotency_key, if_match=if_match, exact_etag=True
            ),
            ok=(200, 202),
        )

    def restore_version(
        self,
        share_id: str,
        path: str,
        version_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/versions/restore",
            body={"path": path, "version_id": version_id},
            headers=self._write_headers(
                idempotency_key=idempotency_key, if_match=if_match, exact_etag=True
            ),
            ok=(200, 202),
        )

    def set_field_value(
        self,
        share_id: str,
        path: str,
        field_id: str,
        value: Any,
        *,
        if_match: str,
    ) -> ApiResponse:
        return self._request(
            "PUT",
            self._share(share_id) + "/field-values/" + self._identifier(field_id),
            body={"path": path, "value": value},
            headers=self._write_headers(if_match=if_match),
        )

    def create_upload_session(
        self,
        share_id: str,
        path: str,
        *,
        size: int,
        content_type: str | None = None,
        on_conflict: str | None = None,
    ) -> ApiResponse:
        return self._request(
            "POST",
            "/uploads",
            body=self._body(
                share_id=share_id,
                path=path,
                size=size,
                content_type=content_type,
                on_conflict=on_conflict,
            ),
            ok=(201,),
        )

    def complete_upload_session(self, session_id: str) -> ApiResponse:
        return self._request(
            "POST", "/uploads/" + self._identifier(session_id) + "/complete", ok=(201,)
        )

    def cancel_upload_session(self, session_id: str) -> ApiResponse:
        return self._request(
            "POST", "/uploads/" + self._identifier(session_id) + "/cancel", ok=(204,)
        )

    def create_share_request(
        self,
        share_id: str,
        path: str,
        *,
        kind: str,
        idempotency_key: str,
        people: list[str] | None = None,
        permission: str | None = None,
        recipient_emails: list[str] | None = None,
        expiry_days: int | None = None,
        message: str | None = None,
    ) -> ApiResponse:
        return self._request(
            "POST",
            self._share(share_id) + "/share-requests",
            body=self._body(
                path=path,
                kind=kind,
                people=people,
                permission=permission,
                recipient_emails=recipient_emails,
                expiry_days=expiry_days,
                message=message,
            ),
            headers=self._write_headers(idempotency_key=idempotency_key),
            ok=(202,),
        )

    def get_share_request(self, request_id: str) -> ApiResponse:
        return self._request("GET", "/share-requests/" + self._identifier(request_id))

    def list_share_requests(self, *, cursor: str | None = None, limit: int | None = None) -> Page:
        response = self._request(
            "GET",
            "/share-requests",
            params={"cursor": cursor, "limit": limit},
        )
        return Page(response.data, response.headers)

    def iter_share_request_pages(self, *, cursor: str | None = None, limit: int | None = None):
        return self._pages(self.list_share_requests, cursor=cursor, limit=limit)

    def get_operation(self, operation_id: str) -> ApiResponse:
        """One poll: 202 while pending, 200 on success, provider error at termination."""
        return self._request("GET", "/operations/" + self._identifier(operation_id), ok=(200, 202))

    def list_operations(self, *, cursor: str | None = None, limit: int | None = None) -> Page:
        response = self._request("GET", "/operations", params={"cursor": cursor, "limit": limit})
        return Page(response.data, response.headers)

    def iter_operation_pages(self, *, cursor: str | None = None, limit: int | None = None):
        return self._pages(self.list_operations, cursor=cursor, limit=limit)

    def cancel_operation(self, operation_id: str) -> ApiResponse:
        return self._request(
            "POST", "/operations/" + self._identifier(operation_id) + "/cancel", ok=(204,)
        )

    def head(
        self,
        operation: str,
        *,
        share_id: str | None = None,
        identifier: str | None = None,
        params: dict[str, Any] | None = None,
        if_none_match: str | None = None,
        byte_range: str | None = None,
    ) -> ApiResponse:
        """HEAD any authenticated read by documented operationId, without redirects.

        Supply share_id for share reads, identifier for getOperation/getShareRequest,
        and query params (path, q, cursor, limit, direct) as documented for the GET.
        """
        if byte_range is not None and operation != "getContent":
            raise ValueError("byte_range is only supported for getContent HEAD")
        share_suffixes = {
            "listChildren": "/children",
            "getItem": "/item",
            "getContent": "/content",
            "getDownloadUrl": "/download-url",
            "getFieldValues": "/field-values",
            "listVersions": "/versions",
        }
        plain = {
            "getMe": "/me",
            "listShares": "/shares",
            "search": "/search",
            "listOperations": "/operations",
        }
        if operation in share_suffixes:
            if share_id is None:
                raise ValueError("share_id is required for this HEAD operation")
            endpoint = self._share(share_id) + share_suffixes[operation]
        elif operation in plain:
            endpoint = plain[operation]
        elif operation in ("getOperation", "getShareRequest"):
            if identifier is None:
                raise ValueError("identifier is required for this HEAD operation")
            endpoint = "/operations/" if operation == "getOperation" else "/share-requests/"
            endpoint += self._identifier(identifier)
        elif operation == "getShare":
            if share_id is None:
                raise ValueError("share_id is required for this HEAD operation")
            endpoint = self._share(share_id)
        elif operation in ("listFields", "listShareRequests"):
            endpoint = "/fields" if operation == "listFields" else "/share-requests"
        else:
            raise ValueError("Unknown authenticated GET operationId")
        ok = (200, 202) if operation == "getOperation" else (200,)
        if operation == "getContent":
            ok = (200, 206, 302, 304)
        elif operation in ("getItem", "getFieldValues"):
            ok = (200, 304)
        headers = {"If-None-Match": if_none_match} if if_none_match else {}
        if byte_range is not None:
            headers["Range"] = byte_range
        return self._request(
            "HEAD",
            endpoint,
            params=params,
            ok=ok,
            headers=headers,
        )

    def list_shares(self, *, cursor: str | None = None, limit: int | None = None) -> Page:
        return Page(*self._get("/shares", cursor=cursor, limit=limit))

    def list_children(
        self,
        share_id: str,
        path: str = "/",
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page:
        return Page(
            *self._get(self._share(share_id) + "/children", path=path, cursor=cursor, limit=limit)
        )

    def search(
        self,
        q: str,
        *,
        share_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page:
        return Page(*self._get("/search", q=q, share_id=share_id, cursor=cursor, limit=limit))

    @staticmethod
    def _pages(fetch, **params) -> Iterator[Page]:
        while True:
            page = fetch(**params)
            yield page
            if page.next_cursor is None:
                return
            params["cursor"] = page.next_cursor

    def iter_share_pages(self, *, cursor: str | None = None, limit: int | None = None):
        return self._pages(self.list_shares, cursor=cursor, limit=limit)

    def iter_children_pages(
        self,
        share_id: str,
        path: str = "/",
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ):
        return self._pages(
            self.list_children, share_id=share_id, path=path, cursor=cursor, limit=limit
        )

    def iter_search_pages(
        self,
        q: str,
        *,
        share_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ):
        return self._pages(self.search, q=q, share_id=share_id, cursor=cursor, limit=limit)

    @contextmanager
    def stream_content(
        self,
        share_id: str,
        path: str,
        *,
        byte_range: str | None = None,
        direct: bool | None = None,
        if_none_match: str | None = None,
    ) -> Iterator[ContentStream]:
        """Open content (200/206/304), following up to five credential-free redirects.

        Omit direct to honor the share's configured download behavior.
        Foldr chooses the export format for native documents. Inspect status and
        headers before appending a range: storage may answer 200, ignoring Range.
        """
        headers = {"Authorization": "Bearer " + self._access_token, "Accept-Encoding": "identity"}
        if byte_range is not None:
            headers["Range"] = byte_range
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        response = self._connection.send(
            "GET",
            self.api_url + self._share(share_id) + "/content",
            headers=headers,
            params={
                "path": path,
                **({"direct": str(direct).lower()} if direct is not None else {}),
            },
            operation="Content read",
        )
        try:
            try:
                redirects = 0
                while response.status_code in (301, 302, 303, 307, 308):
                    if redirects >= 5 or "location" not in response.headers:
                        raise ProtocolError("Content redirect limit reached or Location missing")
                    target = urljoin(str(response.url), response.headers["location"])
                    parsed = urlsplit(target)
                    if (
                        parsed.scheme != "https"
                        or not parsed.hostname
                        or parsed.username is not None
                        or parsed.password is not None
                    ):
                        raise ProtocolError(
                            "Content redirect must use HTTPS without URL credentials"
                        )
                    check_destination(target, self._destination_policy)
                    response.close()
                    # Never restore credentials, even after a same-origin hop or a
                    # redirect back to Foldr. No session cookies are inherited.
                    public_headers = {k: v for k, v in headers.items() if k != "Authorization"}
                    response = self._connection.send(
                        "GET",
                        target,
                        headers=public_headers,
                        operation="Content redirect",
                    )
                    redirects += 1
                if response.status_code not in (200, 206, 304):
                    raise_response(response)
                if (
                    response.status_code != 304
                    and response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ProtocolError("Content response must honor Accept-Encoding: identity")
            except httpx.HTTPError:
                raise TransportError("Content read") from None
            yield ContentStream(response)
        finally:
            response.close()
