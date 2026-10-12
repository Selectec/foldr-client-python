"""Locally maintained request regressions with deliberately synthetic responses.

These cases pin the client's intended wire behavior independently of its route
implementation. They do not load a provider schema or verify a live service.
"""

import json
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import pytest

from foldr_client import ApiResponse, FoldrClient, FoldrSite, Page

SITE = "https://caller-selected.foldr.example"
SHARE = "11111111-1111-4111-8111-111111111111"
OTHER_SHARE = "22222222-2222-4222-8222-222222222222"
FIELD = "33333333-3333-4333-8333-333333333333"
OPAQUE = "opaque +/id"
PATH = "/Reports/Caf\u00e9 #1.xlsx"
ETAG = 'W/"specific-version"'
KEY = "caller-controlled-idempotency-key"
VALUES = {
    "share_id": SHARE,
    "field_id": FIELD,
    "id": OPAQUE,
    "path": PATH,
    "q": "budget & forecast",
    "cursor": "opaque +/=\u00fc&%",
    "limit": 17,
    "direct": False,
    "to_share": OTHER_SHARE,
    "to_path": "/Archive/Canonical copy.xlsx",
    "new_name": "Canonical copy.xlsx",
    "on_conflict": "rename",
    "version_id": "opaque +/version",
    "value": False,
    "kind": "people",
    "people": ["person@example.com", "Exact group name"],
    "permission": "write",
    "recipient_emails": ["outside@example.net"],
    "expiry_days": -1,
    "message": "Please review",
    "size": 3,
    "content_type": "text/plain",
}
PAGING = {"cursor": VALUES["cursor"], "limit": VALUES["limit"]}
PRECONDITIONS = {"if_match": ETAG, "idempotency_key": KEY}
# Public calls with minimum inputs and a second set of optional inputs.
# Expected wire requests are maintained separately below.
CALLS = {
    "getMe": ("me", (), {}, {}),
    "listShares": ("list_shares", (), {}, PAGING),
    "getShare": ("get_share", (SHARE,), {}, {}),
    "listChildren": ("list_children", (SHARE, PATH), {}, PAGING),
    "getItem": ("get_item_response", (SHARE, PATH), {}, {"if_none_match": ETAG}),
    "search": ("search", (VALUES["q"],), {}, {"share_id": SHARE, **PAGING}),
    "getContent": (
        "stream_content",
        (SHARE, PATH),
        {},
        {"direct": False, "byte_range": "bytes=0-2", "if_none_match": ETAG},
    ),
    "getDownloadUrl": ("get_download_url", (SHARE, PATH), {}, {}),
    "listFields": ("list_fields", (), {}, {}),
    "getFieldValues": ("get_field_values", (SHARE, PATH), {}, {"if_none_match": ETAG}),
    "setFieldValue": ("set_field_value", (SHARE, PATH, FIELD, False), {"if_match": ETAG}, {}),
    "listVersions": ("list_versions", (SHARE, PATH), {}, {}),
    "createFolder": (
        "create_folder",
        (SHARE, PATH),
        {"idempotency_key": KEY},
        {"on_conflict": "rename"},
    ),
    "uploadFile": (
        "upload_file",
        (SHARE, PATH, b"abc"),
        {"content_length": 3, "idempotency_key": KEY},
        {"on_conflict": "rename"},
    ),
    "overwriteFile": (
        "overwrite_file",
        (SHARE, PATH, b"abc"),
        {"content_length": 3, "if_match": ETAG},
        {},
    ),
    "renameItem": ("rename_item", (SHARE, PATH, VALUES["new_name"]), {"if_match": ETAG}, {}),
    "copyItem": (
        "copy_item",
        (SHARE, PATH),
        {"to_share": OTHER_SHARE, "to_path": VALUES["to_path"], "idempotency_key": KEY},
        {"on_conflict": "rename", "respond_async": True},
    ),
    "moveItem": (
        "move_item",
        (SHARE, PATH),
        {"to_path": VALUES["to_path"], **PRECONDITIONS},
        {"to_share": OTHER_SHARE, "on_conflict": "rename", "respond_async": True},
    ),
    "deleteItem": ("delete_item", (SHARE, PATH), PRECONDITIONS, {}),
    "restoreVersion": (
        "restore_version",
        (SHARE, PATH, VALUES["version_id"]),
        PRECONDITIONS,
        {},
    ),
    "createUploadSession": (
        "create_upload_session",
        (SHARE, PATH),
        {"size": 3},
        {"content_type": "text/plain", "on_conflict": "rename"},
    ),
    "completeUploadSession": ("complete_upload_session", (OPAQUE,), {}, {}),
    "cancelUploadSession": ("cancel_upload_session", (OPAQUE,), {}, {}),
    "createShareRequest": (
        "create_share_request",
        (SHARE, PATH),
        {"kind": "people", "idempotency_key": KEY},
        {
            name: VALUES[name]
            for name in ("people", "permission", "recipient_emails", "expiry_days", "message")
        },
    ),
    "getShareRequest": ("get_share_request", (OPAQUE,), {}, {}),
    "listShareRequests": ("list_share_requests", (), {}, PAGING),
    "getOperation": ("get_operation", (OPAQUE,), {}, {}),
    "listOperations": ("list_operations", (), {}, PAGING),
    "cancelOperation": ("cancel_operation", (OPAQUE,), {}, {}),
}


@dataclass(frozen=True)
class RequestCase:
    method: str
    path: str
    statuses: tuple[int, ...] = (200,)
    response: str = "object"
    query: str = ""
    optional_query: str = ""
    body: str = ""
    optional_body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    optional_headers: dict[str, str] = field(default_factory=dict)


IDEMPOTENCY = {"Idempotency-Key": KEY}
MATCH = {"If-Match": ETAG}
CONDITIONAL = {"If-None-Match": ETAG}
ASYNC = {"Prefer": "respond-async"}

# These expectations are explicit test data, never imported from client code or
# generated by inspecting the calls. Space-separated names select VALUES above.
CASES = {
    "getMe": RequestCase("GET", "/me"),
    "listShares": RequestCase("GET", "/shares", response="page", optional_query="cursor limit"),
    "getShare": RequestCase("GET", "/shares/{share_id}"),
    "listChildren": RequestCase(
        "GET",
        "/shares/{share_id}/children",
        response="page",
        query="path",
        optional_query="cursor limit",
    ),
    "getItem": RequestCase(
        "GET",
        "/shares/{share_id}/item",
        statuses=(200, 304),
        query="path",
        optional_headers=CONDITIONAL,
    ),
    "search": RequestCase(
        "GET", "/search", response="page", query="q", optional_query="share_id cursor limit"
    ),
    "getContent": RequestCase(
        "GET",
        "/shares/{share_id}/content",
        statuses=(200, 206, 302, 304),
        response="bytes",
        query="path",
        optional_query="direct",
        optional_headers={**CONDITIONAL, "Range": "bytes=0-2"},
    ),
    "getDownloadUrl": RequestCase("GET", "/shares/{share_id}/download-url", query="path"),
    "listFields": RequestCase("GET", "/fields", response="array"),
    "getFieldValues": RequestCase(
        "GET",
        "/shares/{share_id}/field-values",
        statuses=(200, 304),
        response="array",
        query="path",
        optional_headers=CONDITIONAL,
    ),
    "setFieldValue": RequestCase(
        "PUT",
        "/shares/{share_id}/field-values/{field_id}",
        response="array",
        body="path value",
        headers=MATCH,
    ),
    "listVersions": RequestCase(
        "GET", "/shares/{share_id}/versions", response="page", query="path"
    ),
    "createFolder": RequestCase(
        "POST",
        "/shares/{share_id}/folders",
        statuses=(201,),
        body="path",
        optional_body="on_conflict",
        headers=IDEMPOTENCY,
    ),
    "uploadFile": RequestCase(
        "PUT",
        "/shares/{share_id}/files",
        statuses=(201,),
        query="path",
        optional_query="on_conflict",
        body="bytes",
        headers={**IDEMPOTENCY, "If-None-Match": "*"},
    ),
    "overwriteFile": RequestCase(
        "PUT", "/shares/{share_id}/content", query="path", body="bytes", headers=MATCH
    ),
    "renameItem": RequestCase(
        "POST", "/shares/{share_id}/rename", body="path new_name", headers=MATCH
    ),
    "copyItem": RequestCase(
        "POST",
        "/shares/{share_id}/copy",
        statuses=(201, 202),
        body="path to_share to_path",
        optional_body="on_conflict",
        headers=IDEMPOTENCY,
        optional_headers=ASYNC,
    ),
    "moveItem": RequestCase(
        "POST",
        "/shares/{share_id}/move",
        statuses=(200, 202),
        body="path to_path",
        optional_body="to_share on_conflict",
        headers={**IDEMPOTENCY, **MATCH},
        optional_headers=ASYNC,
    ),
    "deleteItem": RequestCase(
        "DELETE",
        "/shares/{share_id}/items",
        statuses=(200, 202),
        query="path",
        headers={**IDEMPOTENCY, **MATCH},
    ),
    "restoreVersion": RequestCase(
        "POST",
        "/shares/{share_id}/versions/restore",
        statuses=(200, 202),
        body="path version_id",
        headers={**IDEMPOTENCY, **MATCH},
    ),
    "createUploadSession": RequestCase(
        "POST",
        "/uploads",
        statuses=(201,),
        body="share_id path size",
        optional_body="content_type on_conflict",
    ),
    "completeUploadSession": RequestCase("POST", "/uploads/{id}/complete", statuses=(201,)),
    "cancelUploadSession": RequestCase("POST", "/uploads/{id}/cancel", statuses=(204,)),
    "createShareRequest": RequestCase(
        "POST",
        "/shares/{share_id}/share-requests",
        statuses=(202,),
        body="path kind",
        optional_body="people permission recipient_emails expiry_days message",
        headers=IDEMPOTENCY,
    ),
    "getShareRequest": RequestCase("GET", "/share-requests/{id}"),
    "listShareRequests": RequestCase(
        "GET", "/share-requests", response="page", optional_query="cursor limit"
    ),
    "getOperation": RequestCase("GET", "/operations/{id}", statuses=(200, 202)),
    "listOperations": RequestCase(
        "GET", "/operations", response="page", optional_query="cursor limit"
    ),
    "cancelOperation": RequestCase("POST", "/operations/{id}/cancel", statuses=(204,)),
}


def expected_path(case):
    path = case.path
    for name in ("share_id", "field_id", "id"):
        path = path.replace("{" + name + "}", quote(VALUES[name], safe=""))
    return "/api/v1" + path


def selected_values(required, extra, optional):
    names = required.split() + (extra.split() if optional else [])
    return {name: VALUES[name] for name in names}


def check_request(request, case, optional):
    assert request.method == case.method
    assert request.url.host == "caller-selected.foldr.example"
    assert request.url.raw_path.split(b"?", 1)[0].decode() == expected_path(case)
    assert request.headers["authorization"] == "Bearer synthetic-caller-token"
    assert "cookie" not in request.headers
    if case.response != "bytes":
        assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    query = selected_values(case.query, case.optional_query, optional)
    assert dict(request.url.params) == {
        name: str(value).lower() if isinstance(value, bool) else str(value)
        for name, value in query.items()
    }
    expected_headers = {**case.headers, **(case.optional_headers if optional else {})}
    for name in ("If-Match", "If-None-Match", "Idempotency-Key", "Prefer", "Range"):
        assert request.headers.get(name) == expected_headers.get(name)
    if case.body == "bytes":
        assert request.headers["content-type"] == "application/octet-stream"
        assert request.headers["content-length"] == "3"
        assert request.read() == b"abc"
    elif case.body:
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == selected_values(
            case.body, case.optional_body, optional
        )
    else:
        assert request.content == b""


def response_body(case, status):
    if status in (204, 304) or case.response == "bytes":
        return None
    item = {"id": "synthetic-result", "path": PATH, "future_field": {"preserve": True}}
    if case.response == "array":
        return [item]
    if case.response == "page":
        return {
            "items": [item],
            "next_cursor": None,
            "truncated": False,
            "total_estimate": 1,
            "not_searched": [],
            "future_field": "preserve this too",
        }
    return item


SUCCESS_CASES = [
    (operation_id, status) for operation_id, case in CASES.items() for status in case.statuses
]


def test_request_matrix_covers_all_29_distinct_public_calls():
    assert len(CASES) == 29
    assert set(CALLS) == set(CASES)
    assert len({entry[0] for entry in CALLS.values()}) == 29


@pytest.mark.parametrize("operation_id,status", SUCCESS_CASES)
@pytest.mark.parametrize("optional", [False, True], ids=["minimum-inputs", "optional-inputs"])
def test_request_encoding_and_supported_successes(operation_id, status, optional):
    requests = []
    case = CASES[operation_id]
    body = response_body(case, status)

    def handler(request):
        requests.append(request)
        if request.url.host == "storage.example":
            assert operation_id == "getContent" and status == 302
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            return httpx.Response(200, content=b"abc")
        check_request(request, case, optional)
        if case.response == "bytes":
            if status == 302:
                return httpx.Response(302, headers={"Location": "https://storage.example/file"})
            return httpx.Response(status, content=b"" if status == 304 else b"abc")
        headers = {"ETag": ETAG, "X-Test-Header": "retained"}
        return (
            httpx.Response(status, json=body, headers=headers)
            if body is not None
            else httpx.Response(status, headers=headers)
        )

    method, args, required, extra = CALLS[operation_id]
    kwargs = {**required, **(extra if optional else {})}
    with FoldrClient(
        SITE, access_token="synthetic-caller-token", transport=httpx.MockTransport(handler)
    ) as client:
        if case.response == "bytes":
            with client.stream_content(*args, **kwargs) as stream:
                assert stream.status_code == (200 if status == 302 else status)
                assert b"".join(stream.iter_bytes(2)) == (b"" if status == 304 else b"abc")
        else:
            result = getattr(client, method)(*args, **kwargs)
            if isinstance(result, ApiResponse):
                assert result.status_code == status
                assert result.data == body
                assert result.etag == ETAG
                assert result.headers["x-test-header"] == "retained"
            elif isinstance(result, Page):
                assert result.raw == body
                assert result.items == body["items"]
                assert result.next_cursor is None
                assert result.truncated is False
                assert result.not_searched == []
                assert result.headers["x-test-header"] == "retained"
            else:
                assert result == body
    assert len(requests) == (2 if operation_id == "getContent" and status == 302 else 1)


HEAD_CASES = [
    (operation_id, status)
    for operation_id, case in CASES.items()
    if case.method == "GET"
    for status in case.statuses
]


@pytest.mark.parametrize("operation_id,status", HEAD_CASES)
def test_head_preserves_routes_queries_status_and_headers_without_following_redirects(
    operation_id, status
):
    requests = []
    case = CASES[operation_id]
    query = selected_values(case.query, case.optional_query, True)

    def handler(request):
        requests.append(request)
        assert request.method == "HEAD"
        assert request.url.raw_path.split(b"?", 1)[0].decode() == expected_path(case)
        assert dict(request.url.params) == {
            name: str(value).lower() if isinstance(value, bool) else str(value)
            for name, value in query.items()
        }
        assert request.headers["authorization"] == "Bearer synthetic-caller-token"
        assert "cookie" not in request.headers
        assert request.headers["if-none-match"] == ETAG
        assert request.headers.get("range") == (
            "bytes=0-2" if operation_id == "getContent" else None
        )
        return httpx.Response(
            status,
            headers={
                "ETag": ETAG,
                "Content-Length": "123",
                "Retry-After": "5",
                "Location": "https://storage.example/file",
            },
        )

    kwargs = {"params": query, "if_none_match": ETAG}
    if "{share_id}" in case.path:
        kwargs["share_id"] = SHARE
    if "{id}" in case.path:
        kwargs["identifier"] = OPAQUE
    if operation_id == "getContent":
        kwargs["byte_range"] = "bytes=0-2"
    with FoldrClient(
        SITE, access_token="synthetic-caller-token", transport=httpx.MockTransport(handler)
    ) as client:
        result = client.head(operation_id, **kwargs)
    assert result.status_code == status
    assert result.data is None
    assert result.etag == ETAG
    assert result.headers["content-length"] == "123"
    assert result.retry_after == "5"
    assert result.location == "https://storage.example/file"
    assert len(requests) == 1


def test_public_openapi_retrieval_preserves_synthetic_document_without_credentials():
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic test API", "version": "0.0.0"},
        "paths": {},
        "future_extension": {"preserve": True},
    }
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/openapi.json"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, json=document, headers={"ETag": ETAG})

    with FoldrSite(SITE, transport=httpx.MockTransport(handler)) as site:
        result = site.openapi()
    assert result.data == document
    assert result.etag == ETAG
    assert len(requests) == 1


@pytest.mark.parametrize("direct", [None, False, True])
def test_content_direct_omission_and_explicit_override(direct):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == expected_path(CASES["getContent"])
        assert request.url.params["path"] == PATH
        if direct is None:
            assert "direct" not in request.url.params
        else:
            assert request.url.params["direct"] == str(direct).lower()
        return httpx.Response(200, content=b"abc")

    with FoldrClient(
        SITE, access_token="synthetic-caller-token", transport=httpx.MockTransport(handler)
    ) as client:
        kwargs = {} if direct is None else {"direct": direct}
        with client.stream_content(SHARE, PATH, **kwargs) as stream:
            assert b"".join(stream.iter_bytes()) == b"abc"
    assert len(requests) == 1
