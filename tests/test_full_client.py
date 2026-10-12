"""Behavioral tests for writes, operations, custom fields and HEAD reads."""

import io
import json

import httpx
import pytest

from foldr_client import FoldrClient, FoldrError, ProtocolError, TransportError

SITE = "https://foldr.example"
ETAG = 'W/"48213-1806660909"'
KEY = "caller-chosen-key"
PATH = "/Reports/Caf\u00e9 #1.xlsx"
CANONICAL = "/Reports/Caf\u00e9 #1 copy.xlsx"


def test_confirmed_reads_use_client_site_without_fetching_discovery_or_external_servers():
    requests = []
    fields = [{"id": "field", "type": "future-type", "multiple": False, "read_only": True}]

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/v1/fields":
            body = fields
        elif request.url.path == "/api/v1/share-requests":
            body = {"items": [{"id": "req", "status": "pending_approval"}], "next_cursor": None}
        elif request.url.path == "/api/v1/shares/s":
            body = {"id": "s", "name": "Canonical share name", "provider_extension": "kept"}
        else:
            pytest.fail("Ordinary API reads must not discover or fetch a contract")
        return httpx.Response(200, json=body, headers={"ETag": ETAG})

    with FoldrClient(SITE, access_token="secret", transport=httpx.MockTransport(handler)) as client:
        share = client.get_share("s")
        field_response = client.list_fields()
        request_page = client.list_share_requests(limit=17)
    assert share["name"] == "Canonical share name"
    assert share["provider_extension"] == "kept"
    assert field_response.data == fields
    assert field_response.etag == ETAG
    assert request_page.items[0]["status"] == "pending_approval"
    assert [request.url.path for request in requests] == [
        "/api/v1/shares/s",
        "/api/v1/fields",
        "/api/v1/share-requests",
    ]
    assert all(request.url.host == "foldr.example" for request in requests)
    assert all(request.headers["authorization"] == "Bearer secret" for request in requests)
    assert requests[-1].url.params["limit"] == "17"


def test_share_identifier_is_encoded_as_one_opaque_segment():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "opaque/id"})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        assert client.get_share("opaque/id")["id"] == "opaque/id"
    assert requests[0].url.raw_path == b"/api/v1/shares/opaque%2Fid"


@pytest.mark.parametrize("identifier", ["", ".", ".."])
def test_share_identifier_rejects_empty_or_relative_segments_without_http(identifier):
    def handler(request):
        pytest.fail("Invalid identifiers must not make an HTTP request")

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            client.get_share(identifier)


@pytest.mark.parametrize("method", ["get_item_response", "get_field_values"])
@pytest.mark.parametrize("status", [200, 304])
def test_conditional_metadata_and_field_values_keep_headers_and_arrays(method, status):
    requests = []
    body = (
        {"path": PATH, "etag": ETAG}
        if method == "get_item_response"
        else [
            {"field_id": "f", "value": True, "updated_by": None},
            {"field_id": "f2", "value": {"latitude": 51.5, "longitude": -0.1}},
        ]
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json=body if status == 200 else None,
            headers={"ETag": ETAG, "X-Request-Id": "r"},
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = getattr(client, method)("s", PATH, if_none_match=ETAG)
    assert response.status_code == status
    assert response.data == (body if status == 200 else None)
    assert response.etag == ETAG
    assert response.headers["x-request-id"] == "r"
    assert requests[0].headers["if-none-match"] == ETAG
    assert requests[0].url.params["path"] == PATH


def test_versions_preserve_one_page_order_opaque_ids_and_truncation():
    body = {
        "items": [
            {"id": "3HL4kqtJvjVBH_QUKNd.Fb3NUi", "current": True},
            {"id": "mBLNn8a.dZS0QpWuxkmW3J_FT.", "current": False},
        ],
        "next_cursor": None,
        "truncated": False,
    }
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        page = client.list_versions("s", PATH)
    assert page.raw == body
    assert page.items[0]["current"] is True
    assert page.truncated is False
    assert page.next_cursor is None
    assert requests[0].url.path == "/api/v1/shares/s/versions"
    assert requests[0].url.params["path"] == PATH


def test_download_url_preserves_expiry_without_following_or_exposing_signed_url_in_repr():
    requests = []
    body = {
        "url": "https://storage.example/file?signature=private",
        "expires_at": "2026-10-11T12:00:00Z",
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.get_download_url("s", PATH)
    assert response.data == body
    assert "signature" not in repr(response)
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/shares/s/download-url"
    assert requests[0].url.params["path"] == PATH


@pytest.mark.parametrize(
    "name,args,kwargs,method,endpoint,body,status,required_headers",
    [
        (
            "create_folder",
            ("s", PATH),
            {"idempotency_key": KEY, "on_conflict": "rename"},
            "POST",
            "/shares/s/folders",
            {"path": PATH, "on_conflict": "rename"},
            201,
            {"idempotency-key": KEY},
        ),
        (
            "rename_item",
            ("s", PATH, "Caf\u00e9 #1 copy.xlsx"),
            {"if_match": ETAG},
            "POST",
            "/shares/s/rename",
            {"path": PATH, "new_name": "Caf\u00e9 #1 copy.xlsx"},
            200,
            {"if-match": ETAG},
        ),
        (
            "copy_item",
            ("s", PATH),
            {"to_share": "dest", "to_path": PATH, "idempotency_key": KEY, "on_conflict": "rename"},
            "POST",
            "/shares/s/copy",
            {"path": PATH, "to_share": "dest", "to_path": PATH, "on_conflict": "rename"},
            201,
            {"idempotency-key": KEY},
        ),
        (
            "move_item",
            ("s", PATH),
            {"to_path": CANONICAL, "if_match": ETAG, "idempotency_key": KEY},
            "POST",
            "/shares/s/move",
            {"path": PATH, "to_path": CANONICAL},
            200,
            {"if-match": ETAG, "idempotency-key": KEY},
        ),
        (
            "restore_version",
            ("s", PATH, "opaque/version"),
            {"if_match": ETAG, "idempotency_key": KEY},
            "POST",
            "/shares/s/versions/restore",
            {"path": PATH, "version_id": "opaque/version"},
            200,
            {"if-match": ETAG, "idempotency-key": KEY},
        ),
        (
            "set_field_value",
            ("s", PATH, "f", False),
            {"if_match": '"fv-etag"'},
            "PUT",
            "/shares/s/field-values/f",
            {"path": PATH, "value": False},
            200,
            {"if-match": '"fv-etag"'},
        ),
        (
            "create_upload_session",
            ("s", PATH),
            {"size": 1024, "content_type": "text/plain", "on_conflict": "rename"},
            "POST",
            "/uploads",
            {
                "share_id": "s",
                "path": PATH,
                "size": 1024,
                "content_type": "text/plain",
                "on_conflict": "rename",
            },
            201,
            {},
        ),
    ],
)
def test_write_contracts_preserve_canonical_results_and_caller_preconditions(
    name, args, kwargs, method, endpoint, body, status, required_headers
):
    requests = []
    result = (
        [{"field_id": "f", "value": False}]
        if name == "set_field_value"
        else {"path": CANONICAL, "name": "Caf\u00e9 #1 copy.xlsx", "etag": ETAG}
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json=result,
            headers={"ETag": ETAG, "Location": SITE + "/api/v1/shares/s/item?path=canonical"},
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = getattr(client, name)(*args, **kwargs)
    assert response.status_code == status
    assert response.data == result
    assert response.etag == ETAG
    assert response.location == SITE + "/api/v1/shares/s/item?path=canonical"
    assert len(requests) == 1
    assert requests[0].method == method
    assert requests[0].url.path == "/api/v1" + endpoint
    assert json.loads(requests[0].content) == body
    assert requests[0].headers["content-type"] == "application/json"
    for key, value in required_headers.items():
        assert requests[0].headers[key] == value


@pytest.mark.parametrize("method", ["copy_item", "move_item", "delete_item", "restore_version"])
def test_accepted_changes_expose_pending_approval_without_automatically_polling(method):
    requests = []
    operation = {
        "id": "op",
        "state": "pending_approval",
        "kind": "delete",
        "approval": {"status": "pending", "decided_at": None},
        "deletion": None,
        "result": None,
        "progress": {"items_total": None, "bytes_total": None},
    }
    kwargs = {"idempotency_key": KEY}
    args = ("s", PATH)
    if method != "copy_item":
        kwargs["if_match"] = ETAG
    if method in ("copy_item", "move_item"):
        kwargs.update(to_share="destination", to_path=PATH, respond_async=True)
    if method == "restore_version":
        args += ("version-1",)

    def handler(request):
        requests.append(request)
        return httpx.Response(
            202,
            json=operation,
            headers={
                "Retry-After": "60",
                "Location": SITE + "/api/v1/operations/op",
                "Preference-Applied": "respond-async",
            },
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = getattr(client, method)(*args, **kwargs)
    assert response.status_code == 202
    assert response.data == operation
    assert response.retry_after == "60"
    assert response.location.endswith("/operations/op")
    assert len(requests) == 1
    if method in ("copy_item", "move_item"):
        assert requests[0].headers["prefer"] == "respond-async"


def test_synchronous_deletion_preserves_recoverability_and_uses_query_path():
    requests = []
    deletion = {
        "deleted": {"share_id": "s", "path": PATH},
        "items": 4,
        "recoverable": "snapshot",
        "destination": "shadow-copies-if-enabled",
        "where": "the server's shadow copies, if it keeps them",
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=deletion)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.delete_item("s", PATH, if_match=ETAG, idempotency_key=KEY)
    assert response.data == deletion
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/v1/shares/s/items"
    assert requests[0].url.params["path"] == PATH
    assert requests[0].headers["if-match"] == ETAG
    assert requests[0].headers["idempotency-key"] == KEY
    assert requests[0].content == b""


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("people", {"people": ["user@example.com", "Exact group"], "permission": "write"}),
        ("public_link", {"expiry_days": -1}),
        ("secure_link", {"recipient_emails": ["buyer@example.com"], "expiry_days": 14}),
        ("inbox", {"expiry_days": 0}),
    ],
)
def test_share_requests_send_kind_specific_payload_and_preserve_approval(kind, extra):
    requests = []
    body = {
        "id": "req",
        "kind": kind,
        "status": "pending_approval",
        "path": PATH,
        "decided_at": None,
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json=body)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.create_share_request(
            "s", PATH, kind=kind, idempotency_key=KEY, message="The Q1 figures", **extra
        )
    assert response.data == body
    assert response.status_code == 202
    assert "url" not in response.data
    assert requests[0].url.path == "/api/v1/shares/s/share-requests"
    assert requests[0].headers["idempotency-key"] == KEY
    assert json.loads(requests[0].content) == {
        "path": PATH,
        "kind": kind,
        "message": "The Q1 figures",
        **extra,
    }


@pytest.mark.parametrize("status", ["pending_approval", "approved", "declined", "withdrawn"])
def test_share_request_read_preserves_provider_decision(status):
    requests = []
    body = {"id": "req", "status": status, "kind": "secure_link", "decided_at": None}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        assert client.get_share_request("req").data == body
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v1/share-requests/req"
    assert len(requests) == 1


@pytest.mark.parametrize("collection", ["operations", "share_requests"])
def test_operation_and_share_request_paging_keeps_opaque_cursor_and_empty_pages(collection):
    requests = []
    cursor = "opaque +/=\u00fc&%"

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [],
                "next_cursor": cursor if len(requests) == 1 else None,
                "truncated": True,
                "provider_flag": "kept",
            },
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        iterator = (
            client.iter_operation_pages(limit=17)
            if collection == "operations"
            else client.iter_share_request_pages(limit=17)
        )
        assert requests == []
        pages = list(iterator)
    assert len(pages) == 2
    assert all(page.truncated is True for page in pages)
    assert all(page.raw["provider_flag"] == "kept" for page in pages)
    assert "cursor" not in requests[0].url.params
    assert requests[1].url.params["cursor"] == cursor
    assert all(request.url.params["limit"] == "17" for request in requests)
    assert all(
        request.url.path
        == ("/api/v1/operations" if collection == "operations" else "/api/v1/share-requests")
        for request in requests
    )


@pytest.mark.parametrize("state", ["queued", "running", "cancelling", "pending_approval"])
def test_operation_poll_is_one_request_and_preserves_pending_state_and_retry_after(state):
    requests = []
    body = {
        "id": "op",
        "state": state,
        "phase": "copying",
        "result": None,
        "progress": {"items_done": 0, "items_total": None},
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(
            202,
            json=body,
            headers={"Retry-After": "60", "Location": SITE + "/api/v1/operations/op"},
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.get_operation("op")
    assert response.data == body
    assert response.status_code == 202
    assert response.retry_after == "60"
    assert len(requests) == 1


def test_completed_delete_operation_keeps_deletion_separate_from_result():
    deletion = {"deleted": {"path": PATH}, "items": 1, "recoverable": "none"}
    body = {
        "id": "op",
        "kind": "delete",
        "state": "succeeded",
        "result": None,
        "deletion": deletion,
        "approval": {"status": "approved"},
    }
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    ) as client:
        response = client.get_operation("op")
    assert response.data == body
    assert response.data["result"] is None
    assert response.data["deletion"] == deletion


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "source-remains"),
        (409, "copy-incomplete"),
        (409, "operation-cancelled"),
        (403, "change-declined"),
        (409, "change-expired"),
        (412, "changed-since-requested"),
        (409, "requester-not-connected"),
        (503, "copy-interrupted"),
    ],
)
def test_terminal_operation_problems_preserve_operation_and_partial_result(status, code):
    requests = []
    operation = {
        "id": "op",
        "state": "failed",
        "result": {"path": CANONICAL},
        "failures": [{"path": PATH, "code": "download-blocked"}],
        "failures_truncated": True,
        "approval": {"status": "approved"},
    }
    body = {"code": code, "status": status, "operation": operation, "detail": "Provider decision"}

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Retry-After": "5"})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FoldrError) as caught:
            client.get_operation("op")
    assert caught.value.status_code == status
    assert caught.value.code == code
    assert caught.value.problem["operation"] == operation
    assert len(requests) == 1


@pytest.mark.parametrize(
    "name,endpoint,status",
    [
        ("complete_upload_session", "/uploads/opaque%2Fid/complete", 201),
        ("cancel_upload_session", "/uploads/opaque%2Fid/cancel", 204),
        ("cancel_operation", "/operations/opaque%2Fid/cancel", 204),
    ],
)
def test_session_completion_and_cancellation_keep_http_status_and_do_not_poll(
    name, endpoint, status
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, json={"path": CANONICAL} if status == 201 else None, headers={"ETag": ETAG}
        )

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = getattr(client, name)("opaque/id")
    assert response.status_code == status
    assert response.data == ({"path": CANONICAL} if status == 201 else None)
    assert requests[0].method == "POST"
    assert requests[0].url.raw_path.decode() == "/api/v1" + endpoint
    assert requests[0].content == b""
    assert len(requests) == 1


class UploadTransport(httpx.BaseTransport):
    """Unlike MockTransport, inspect upload chunks before HTTPX buffers a request."""

    def __init__(self, *, interrupt=False):
        self.requests = []
        self.chunks = []
        self.interrupt = interrupt

    def handle_request(self, request):
        self.requests.append(request)
        for chunk in request.stream:
            self.chunks.append(chunk)
            if self.interrupt:
                raise httpx.WriteError("upstream body interrupted", request=request)
        status = 201 if request.url.path.endswith("/files") else 200
        return httpx.Response(status, json={"path": CANONICAL}, headers={"ETag": ETAG})


@pytest.mark.parametrize("overwrite", [False, True])
def test_uploads_stream_bounded_chunks_and_leave_caller_file_open(overwrite):
    data = b"original file bytes\n" * 10000

    class CallerFile(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            assert 0 < size <= 65536
            return super().read(size)

    source = CallerFile(data + b"later bytes must not be read")
    transport = UploadTransport()
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        if overwrite:
            response = client.overwrite_file(
                "s", PATH, source, content_length=len(data), if_match=ETAG
            )
        else:
            response = client.upload_file(
                "s",
                PATH,
                source,
                content_length=len(data),
                idempotency_key=KEY,
                on_conflict="rename",
            )
    assert response.data["path"] == CANONICAL
    assert b"".join(transport.chunks) == data
    assert all(len(chunk) <= 65536 for chunk in transport.chunks)
    assert source.closed is False
    assert source.getvalue() == data + b"later bytes must not be read"
    assert source.tell() == len(data)
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "PUT"
    assert request.url.path == "/api/v1/shares/s/" + ("content" if overwrite else "files")
    assert request.url.params["path"] == PATH
    assert request.headers["content-length"] == str(len(data))
    assert request.headers["content-type"] == "application/octet-stream"
    assert "transfer-encoding" not in request.headers
    if overwrite:
        assert request.headers["if-match"] == ETAG
        assert "if-none-match" not in request.headers
    else:
        assert request.headers["if-none-match"] == "*"
        assert request.headers["idempotency-key"] == KEY
        assert request.url.params["on_conflict"] == "rename"
        assert "if-match" not in request.headers


def test_interrupted_upload_is_uncertain_not_retried_or_rewound_and_caller_keeps_file():
    source = io.BytesIO(b"x" * 200000)
    transport = UploadTransport(interrupt=True)
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        with pytest.raises(TransportError) as caught:
            client.upload_file("s", PATH, source, content_length=200000, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert len(transport.requests) == 1
    assert len(transport.chunks) == 1
    assert source.tell() == 65536
    assert source.closed is False
    assert source.getvalue() == b"x" * 200000


def test_caller_file_read_failure_is_uncertain_and_leaves_file_open_without_retry():
    class FailingFile(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("caller disk read failed at private local path")
            return super().read(min(size, 3))

    source = FailingFile(b"abcdef")
    transport = UploadTransport()
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        with pytest.raises(TransportError) as caught:
            client.upload_file("s", PATH, source, content_length=6, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert "private local path" not in str(caught.value)
    assert len(transport.requests) == 1
    assert transport.chunks == [b"abc"]
    assert source.tell() == 3
    assert source.closed is False
    assert source.getvalue() == b"abcdef"
    # The file failed before the fake server could return any HTTP response.


def test_short_caller_file_reports_uncertain_protocol_failure_and_remains_open():
    source = io.BytesIO(b"abc")
    transport = UploadTransport()
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        with pytest.raises(ProtocolError) as caught:
            client.upload_file("s", PATH, source, content_length=4, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert len(transport.requests) == 1
    assert transport.chunks == [b"abc"]
    assert source.tell() == 3
    assert source.closed is False
    assert source.getvalue() == b"abc"


def test_upload_response_interruption_closes_response_and_leaves_caller_file_open():
    class InterruptedResponse(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"path": '
            raise httpx.ReadError("response interrupted after upload")

        def close(self):
            self.closed = True

    source = io.BytesIO(b"abc")
    response_body = InterruptedResponse()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.content == b"abc"
        return httpx.Response(201, stream=response_body)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransportError) as caught:
            client.upload_file("s", PATH, source, content_length=3, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert len(requests) == 1
    assert source.closed is False
    assert source.getvalue() == b"abc"
    assert response_body.closed


def test_overlong_caller_iterator_reports_uncertain_protocol_failure_without_closing_source():
    class CallerIterator:
        closed = False

        def __init__(self):
            self.items = iter([b"abc", b"de"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.items)

        def close(self):
            self.closed = True

    source = CallerIterator()
    transport = UploadTransport()
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        with pytest.raises(ProtocolError) as caught:
            client.upload_file("s", PATH, source, content_length=4, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert len(transport.requests) == 1
    assert transport.chunks == [b"abc"]
    assert source.closed is False


@pytest.mark.parametrize("source,length", [(iter([b"abc"]), 4), (iter([b"abcde"]), 4)])
def test_upload_iterator_length_mismatch_reports_uncertain_failure_without_retry(source, length):
    transport = UploadTransport()
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        with pytest.raises(ProtocolError) as caught:
            client.upload_file("s", PATH, source, content_length=length, idempotency_key=KEY)
    assert caught.value.outcome_uncertain is True
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "operation,kwargs,endpoint",
    [
        ("getMe", {}, "/me"),
        ("listShares", {}, "/shares"),
        ("search", {}, "/search"),
        ("listChildren", {"share_id": "s"}, "/shares/s/children"),
        ("getItem", {"share_id": "s"}, "/shares/s/item"),
        ("getContent", {"share_id": "s"}, "/shares/s/content"),
        ("getDownloadUrl", {"share_id": "s"}, "/shares/s/download-url"),
        ("getFieldValues", {"share_id": "s"}, "/shares/s/field-values"),
        ("listVersions", {"share_id": "s"}, "/shares/s/versions"),
        ("getOperation", {"identifier": "op"}, "/operations/op"),
        ("listOperations", {}, "/operations"),
        ("getShareRequest", {"identifier": "req"}, "/share-requests/req"),
        ("getShare", {"share_id": "s"}, "/shares/s"),
        ("listFields", {}, "/fields"),
        ("listShareRequests", {}, "/share-requests"),
    ],
)
def test_head_supports_every_authenticated_get_without_parsing_body(operation, kwargs, endpoint):
    requests = []

    class BodyMustNotBeRead(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("HEAD must not consume response body")
            yield b""

        def close(self):
            self.closed = True

    source = BodyMustNotBeRead()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=source, headers={"ETag": ETAG, "Content-Length": "1234"})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.head(operation, params={"path": PATH}, **kwargs)
    assert response.status_code == 200
    assert response.data is None
    assert response.etag == ETAG
    assert response.headers["content-length"] == "1234"
    assert len(requests) == 1
    assert requests[0].method == "HEAD"
    assert requests[0].url.path == "/api/v1" + endpoint
    assert requests[0].url.params["path"] == PATH
    assert source.closed


def test_head_content_returns_redirect_location_without_spending_a_storage_request():
    requests = []
    signed_url = "https://storage.example/file?signature=private"

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": signed_url})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.head("getContent", share_id="s", params={"path": PATH})
    assert response.status_code == 302
    assert response.location == signed_url
    assert response.data is None
    assert "signature" not in repr(response)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [206, 302])
def test_head_range_preserves_partial_headers_without_reading_body_or_following_redirect(status):
    requests = []

    class BodyMustNotBeRead(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("HEAD range must not consume any content")
            yield b""

        def close(self):
            self.closed = True

    source = BodyMustNotBeRead()
    headers = {"Content-Range": "bytes 10-19/100", "Content-Length": "10", "ETag": ETAG}
    if status == 302:
        headers["Location"] = "https://storage.example/range?signature=private"

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=source)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.head(
            "getContent", share_id="s", params={"path": PATH}, byte_range="bytes=10-19"
        )
    assert response.status_code == status
    assert response.data is None
    assert response.headers["content-range"] == "bytes 10-19/100"
    assert response.headers["content-length"] == "10"
    assert requests[0].method == "HEAD"
    assert requests[0].headers["range"] == "bytes=10-19"
    assert len(requests) == 1
    assert source.closed


@pytest.mark.parametrize(
    "operation,kwargs",
    [
        ("getMe", {}),
        ("getItem", {"share_id": "s"}),
        ("listFields", {}),
    ],
)
def test_head_rejects_range_for_other_reads_before_sending_any_http(operation, kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        pytest.fail("Range on another HEAD read must be rejected before any HTTP request")

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            client.head(operation, byte_range="bytes=10-19", **kwargs)
    assert requests == []


@pytest.mark.parametrize(
    "operation,kwargs",
    [
        ("getItem", {"share_id": "s"}),
        ("getFieldValues", {"share_id": "s"}),
    ],
)
def test_head_conditional_metadata_preserves_304(operation, kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(304, headers={"ETag": ETAG})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        response = client.head(operation, params={"path": PATH}, if_none_match=ETAG, **kwargs)
    assert response.status_code == 304
    assert response.data is None
    assert requests[0].headers["if-none-match"] == ETAG


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("overwrite_file", ("s", PATH, b"x"), {"content_length": 1, "if_match": "*"}),
        ("move_item", ("s", PATH), {"to_path": CANONICAL, "if_match": "*", "idempotency_key": KEY}),
        ("delete_item", ("s", PATH), {"if_match": "*", "idempotency_key": KEY}),
        ("restore_version", ("s", PATH, "version"), {"if_match": "*", "idempotency_key": KEY}),
        ("create_folder", ("s", PATH), {"idempotency_key": ""}),
        ("create_folder", ("s", PATH), {"idempotency_key": "contains space"}),
    ],
)
def test_invalid_local_preconditions_fail_without_sending_a_mutation(method, args, kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        pytest.fail("Invalid local preconditions must not send a mutation")

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            getattr(client, method)(*args, **kwargs)
    assert requests == []
