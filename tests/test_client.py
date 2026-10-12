"""Contract tests using HTTPX responses; no Foldr deployment is contacted."""

import httpx
import pytest

from foldr_client import FoldrClient, FoldrError, ProtocolError, TransportError

SITE = "https://foldr.example"


def test_account_and_resources_preserve_provider_fields_and_canonical_paths():
    canonical = "/Reports/Caf\u00e9 #1/Results.xlsx"
    account = {"id": "person-1", "name": "Ada", "provider_extension": {"x": 1}}
    share = {"id": "share-1", "name": "Reports", "read_only": False}
    item = {
        "share_id": "share-1",
        "path": canonical,
        "name": "Results.xlsx",
        "kind": "file",
        "etag": '"etag-1"',
        "read_only": True,
        "checked_out": {"owner": "person-2"},
        "provider_extension": [1, 2],
    }
    responses = [
        account,
        {"items": [share], "next_cursor": None, "truncated": False},
        {"items": [item], "next_cursor": None, "truncated": False},
        item,
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests) - 1], headers={"X-Trace": "t"})

    with FoldrClient(
        SITE, access_token="identity", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.me() == account
        shares = client.list_shares(limit=37)
        children = client.list_children("share-1", "/reports/cafe #1", limit=12)
        metadata = client.get_item("share-1", children.items[0]["path"])
    assert shares.raw == responses[1]
    assert shares.headers["x-trace"] == "t"
    assert shares.truncated is False
    assert children.items == [item]
    assert metadata == item
    assert [request.url.path for request in requests] == [
        "/api/v1/me",
        "/api/v1/shares",
        "/api/v1/shares/share-1/children",
        "/api/v1/shares/share-1/item",
    ]
    assert requests[1].url.params["limit"] == "37"
    assert requests[2].url.params["path"] == "/reports/cafe #1"
    assert requests[3].url.params["path"] == canonical
    assert all(request.headers["authorization"] == "Bearer identity" for request in requests)


def test_separate_clients_keep_their_authenticated_identity_and_ignore_response_cookies():
    observed = []

    def handler(request):
        observed.append((request.headers["authorization"], request.headers.get("cookie")))
        return httpx.Response(
            200,
            json={"id": request.headers["authorization"]},
            headers={"Set-Cookie": "session=secret; Path=/"},
        )

    with (
        FoldrClient(SITE, access_token="alice", transport=httpx.MockTransport(handler)) as alice,
        FoldrClient(SITE, access_token="bob", transport=httpx.MockTransport(handler)) as bob,
    ):
        assert alice.me()["id"] == "Bearer alice"
        assert bob.me()["id"] == "Bearer bob"
        assert alice.me()["id"] == "Bearer alice"
    assert observed == [("Bearer alice", None), ("Bearer bob", None), ("Bearer alice", None)]


@pytest.mark.parametrize("collection", ["shares", "children", "search"])
def test_page_iteration_keeps_opaque_cursor_query_and_empty_intermediate_pages(collection):
    cursor = "opaque +/%=&\u00fc"
    requests = []
    bodies = [
        {"items": [], "next_cursor": cursor, "truncated": True},
        {"items": [{"path": "/Canonical"}], "next_cursor": None, "truncated": True},
    ]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=bodies[len(requests) - 1])

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        if collection == "shares":
            pages = client.iter_share_pages(limit=2)
        elif collection == "children":
            pages = client.iter_children_pages("share", "/Mixed Case", limit=2)
        else:
            pages = client.iter_search_pages("quarterly + forecast", share_id="share", limit=2)
        assert requests == []  # Iterators do not fetch until consumed.
        result = list(pages)
    assert [page.raw for page in result] == bodies
    assert "cursor" not in requests[0].url.params
    assert requests[1].url.params["cursor"] == cursor
    assert [request.url.params["limit"] for request in requests] == ["2", "2"]
    if collection == "children":
        assert all(request.url.params["path"] == "/Mixed Case" for request in requests)
    if collection == "search":
        assert all(request.url.params["q"] == "quarterly + forecast" for request in requests)
        assert all(request.url.params["share_id"] == "share" for request in requests)


def test_search_preserves_incomplete_coverage_estimate_and_hit_details():
    body = {
        "items": [
            {"item": {"path": "/Kept Case", "etag": None}, "score": 7.42, "snippet": "forecast"}
        ],
        "next_cursor": None,
        "total_estimate": 134,
        "not_searched": [
            {"share_id": "s1", "name": "Archive", "reason": "unreachable"},
            {"share_id": "s2", "reason": "future-provider-reason", "extra": True},
        ],
        "future_extension": {"kept": True},
    }
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    ) as client:
        page = client.search("forecast")
    assert page.raw == body
    assert page.items == body["items"]
    assert page.not_searched == body["not_searched"]
    assert page.total_estimate == 134
    assert page.truncated is None
    assert page.next_cursor is None  # End of paging does not erase missing coverage.


def test_expired_cursor_surfaces_without_silently_restarting_listing():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json={"items": [], "next_cursor": "snapshot", "truncated": False}
            )
        return httpx.Response(410, json={"code": "cursor-expired", "detail": "Start again"})

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        pages = client.iter_children_pages("s")
        assert next(pages).next_cursor == "snapshot"
        with pytest.raises(FoldrError) as caught:
            next(pages)
    assert caught.value.status_code == 410
    assert caught.value.code == "cursor-expired"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "invalid-token"),
        (403, "insufficient-scope"),
        (404, "not-found"),
        (400, "invalid-cursor"),
        (429, "rate-limited"),
        (503, "share-unreachable"),
        (409, "storage-account-not-connected"),
    ],
)
def test_structured_provider_errors_are_preserved_without_retries(status, code):
    problem = {
        "type": "https://foldr.example/problems/" + code,
        "status": status,
        "code": code,
        "detail": "Provider detail",
        "instance": "/requests/123",
        "provider": {"reason": "storage policy"},
    }
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, json=problem, headers={"Retry-After": "12", "X-Request-Id": "123"}
        )

    with FoldrClient(
        SITE, access_token="never-in-errors", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(FoldrError) as caught:
            client.me()
    error = caught.value
    assert error.status_code == status
    assert error.problem == problem
    assert error.code == code
    assert error.type == problem["type"]
    assert error.detail == problem["detail"]
    assert error.instance == problem["instance"]
    assert error.retry_after == "12"
    assert error.headers["x-request-id"] == "123"
    assert "never-in-errors" not in str(error)
    assert len(requests) == 1


@pytest.mark.parametrize("retry_after", [None, "60"])
def test_html_rate_limit_preserves_headers_and_body_without_json_assumptions(retry_after):
    requests = []
    body = b"<html><h1>Too many requests</h1></html>"

    def handler(request):
        requests.append(request)
        headers = {"Content-Type": "text/html"}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return httpx.Response(429, content=body, headers=headers)

    with FoldrClient(SITE, access_token="a", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FoldrError) as caught:
            client.list_shares()
    assert caught.value.problem is None
    assert caught.value.body == body
    assert caught.value.retry_after == retry_after
    assert len(requests) == 1


def test_transport_failure_is_reported_once_without_leaking_request_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("sensitive upstream text", request=request)

    with FoldrClient(
        SITE, access_token="private-token", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TransportError) as caught:
            client.me()
    assert len(requests) == 1
    assert caught.value.outcome_uncertain is False
    assert "private-token" not in str(caught.value)
    assert "sensitive upstream text" not in str(caught.value)


@pytest.mark.parametrize("body", [[], {"items": []}, {"items": [], "next_cursor": 42}])
def test_malformed_page_is_a_protocol_error(body):
    with FoldrClient(
        SITE,
        access_token="a",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    ) as client:
        with pytest.raises(ProtocolError):
            client.list_shares()


def test_context_manager_closes_owned_http_transport():
    class OwnedTransport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    transport = OwnedTransport(lambda request: httpx.Response(200, json={"id": "a"}))
    with FoldrClient(SITE, access_token="a", transport=transport) as client:
        assert client.me() == {"id": "a"}
        assert transport.closed is False
    assert transport.closed is True
