# Foldr Python client

A thin, synchronous, standalone Python client for the Foldr API v1. Python 3.10+
is required.

The client provides 29 authenticated API operations: reads,
search, downloads, writes, versions, custom fields, direct upload sessions,
operations and sharing requests. OAuth includes authorization, renewal,
revocation, dynamic registration and both discovery documents. All authenticated
reads support HEAD. See the [operation inventory](docs/api-reference.md).

Methods use fixed API routes; constructing a client opens no network connection.

The caller supplies a Foldr HTTPS site and an access token. Each client has its
own HTTP session and identity. Foldr decides which shares and files that identity
may access, including shares backed by another storage account.
Access tokens must be nonempty visible ASCII without whitespace; invalid values
are rejected before any request.

## Install

From a checkout:

```console
python -m pip install .
```

Or install a built wheel:

```console
python -m pip install /path/to/foldr_client-0.1.0-py3-none-any.whl
```

The distribution is named `foldr-client`; the import is `foldr_client`.
Runtime dependencies are HTTPX and OAuthLib. Nothing is published by CI.

## Response shapes

`me()`, `get_item()` and `get_share()` return resource dictionaries.
Collection methods return `Page` as described below. Calls that need HTTP
status or headers return `ApiResponse`: `.data` is the exact JSON object or array,
`.status_code` and `.headers` retain the protocol result. `.etag`, `.location` and
`.retry_after` are convenient header accessors. HEAD, 204 and 304 have `.data=None`.
The envelope's repr hides data and headers that may contain signed URLs or secrets.
`get_item_response` supplies this envelope for conditional metadata reads.

## Read files

The paths, application names and addresses in this documentation are synthetic.
Supply your own authorized site, credentials and resource identifiers.

```python
import os
from foldr_client import FoldrClient

with FoldrClient(os.environ["FOLDR_SITE"], access_token=os.environ["FOLDR_ACCESS_TOKEN"]) as client:
    print(client.me())  # requires profile scope
    for page in client.iter_share_pages(limit=100):
        for share in page.items:
            print(share["id"], share["name"], share["reachable"])

    share_id = os.environ["FOLDR_SHARE_ID"]
    for page in client.iter_children_pages(share_id, "/Reports"):
        if page.truncated:
            print("The server marked these results as truncated")
        for item in page.items:
            print(item["path"])  # retain this canonical path verbatim

    item = client.get_item(share_id, "/Reports/Q3.xlsx")
    with client.stream_content(share_id, item["path"]) as stream:
        with open("Q3.xlsx", "wb") as output:
            for chunk in stream.iter_bytes():
                output.write(chunk)
```

Pass the site origin, such as `https://files.example.com`, without `/api/v1`.
Storage reads require `foldr.read`; `/me` requires `profile`. The client sends
paths through the HTTP query encoder without trimming, changing case or Unicode
normalization. Resource dictionaries retain new/unknown Foldr fields and values.

## Search and pages

```python
with FoldrClient(site, access_token=token) as client:
    for page in client.iter_search_pages("quarterly forecast", limit=20):
        # Preserve/report these alongside the hits, including on empty pages.
        print(page.total_estimate, page.not_searched, page.truncated)
        for hit in page.items:
            print(hit["item"]["path"], hit["snippet"])
```

Single-page methods are `list_shares`, `list_children` and `search`. Each accepts
keyword `cursor` and `limit`; search also accepts `share_id`. To manage pagination
yourself, pass `page.next_cursor` unchanged with the same other parameters.
Iterators fetch lazily and yield **pages**, including empty pages, until the cursor
is null. `Page.raw` contains the full response; `Page.headers` contains rate headers.

A null cursor means the page sequence ended; check `truncated` and `not_searched`
before treating results as complete. Missing flags return `None`, not a claim of
completeness. Search totals are estimates, reasons in `not_searched` are open-ended,
and search hits' ETags are not live metadata. A `410 cursor-expired` is raised to
the caller without silently restarting.

## Streaming, ranges and exports

`stream_content(share_id, path, *, byte_range=None, direct=None,
if_none_match=None)` is a context manager. The stream exposes `status_code`,
`headers`, `bytes_received` and `iter_bytes(chunk_size=65536)`. Consume it inside
the context. Early exit, failure and normal completion close the response. Close
clients with `with` or `close()`; do not close a client while consuming a stream.

```python
with client.stream_content(
    share_id, item["path"], byte_range="bytes=0-1023", direct=False
) as stream:
    if stream.status_code == 206:
        print(stream.headers.get("Content-Range"))
    elif stream.status_code == 200:
        print("Whole file returned; do not append this as a resumed range")
    for chunk in stream.iter_bytes(1024):
        consume(chunk)
```

Foldr may ignore a range when storage cannot seek. Inspect the status and
`Content-Range` before resuming; the client never appends, resumes or retries for
you. `if_none_match` sends the supplied ETag unchanged; a 304 has no content.

For exported documents, inspect any `item["export"]` metadata and the response's
`Content-Type` and `Content-Disposition` before choosing a local filename.
The client streams the server's response and provides no export-format selector.

For direct downloads, the client follows at most five redirects, sending **no
Foldr Authorization or cookies** on any redirect hop, including the same host.
Only HTTPS destinations without URL credentials are accepted. Omitted `direct`
honors the share's setting; `direct=False` requests bytes through Foldr.
Choose `direct=False` when downloads need Foldr to check access on each new
request. It does not revoke storage URLs issued earlier; those can outlive an app
disconnect or permission change. An optional `destination_policy` callback on
`FoldrClient` checks every download redirect, and on `DirectUpload` checks each
storage send. See [client safety configuration](docs/client-safety.md).
Signed storage URLs and Location headers are credentials: keep them out of logs.
HTTPX/HTTPCore logs can expose these URLs even when exception messages are safe;
the opt-in `HTTPLogFilter` suppresses their records on configured output handlers.
Stream chunks use bounded buffering;
collecting them into a list or joining them makes memory use the caller's concern.
Content requests require identity encoding. A server that sends compressed content
despite that request is rejected before decompression, preserving bounded memory
and byte-range semantics.

## OAuth

`FoldrOAuth` implements code/PKCE, refresh and revocation
operations. The app must already be registered at that site.

Register and inspect the public site without a token using `FoldrSite`:

```python
from foldr_client import FoldrSite

with FoldrSite(site) as public:
    discovery = public.authorization_metadata().data
    resource = public.protected_resource_metadata().data
    contract = public.openapi().data
    registration = public.register(
        {
            "client_name": "Example Reports",
            "redirect_uris": ["https://app.example.com/foldr/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "foldr.read offline_access profile",
        }
    ).data
    # Keep registration securely, including a client_secret if issued.
    # Inspect actual registered values: Foldr can narrow the request.
```

Registration is an explicit mutation, not something construction or discovery
does automatically. Public endpoints never inherit Foldr credentials or cookies.
Discovery returns metadata; it does not redirect authenticated requests to another
site. `FoldrSite.problem_type(slug)` retrieves a public problem description as JSON.

```python
from foldr_client import FoldrClient, FoldrOAuth

with FoldrOAuth(site, client_id=client_id) as oauth:
    authorization = oauth.authorize(
        "https://app.example.com/foldr/callback",
        scopes=["foldr.read", "profile", "offline_access"],
    )
    # Retain authorization securely, bound to the initiating user's session.
    # Direct that user's browser to authorization.url.
    # In the callback, retrieve and consume that context once:
    tokens = oauth.exchange_callback(callback_url, authorization)
    # Check tokens["scope"]: Foldr may grant fewer scopes than requested.
    # Persist tokens securely before using the access token.

with FoldrClient(site, access_token=tokens["access_token"]) as client:
    account = client.me()
```

Authorization uses random state and S256 PKCE, and binds the resource to the site's
`/api/v1`. Callback exchange validates the registered target, state and `iss`
before sending the code. HTTPS callbacks and HTTP loopback callbacks are supported.
The `Authorization` object contains sensitive transient state; its repr hides it.
The caller owns callback/session storage and replay prevention.

For confidential clients, supply `client_secret` and explicitly choose
`auth_method="client_secret_basic"` or `"client_secret_post"`. The default `"none"`
is for public clients. Basic and form client authentication are never combined.

```python
with FoldrOAuth(site, client_id=client_id) as oauth:
    # Caller holds its connection's refresh lock around this operation AND save.
    renewed = oauth.refresh(stored_refresh_token)
    # Persist renewed (especially its new refresh_token) atomically before use.
    # Construct a new FoldrClient with renewed["access_token"].
    # Optional narrower access token: refresh(token, scopes=["foldr.read"])
    oauth.revoke(renewed["refresh_token"], token_type_hint="refresh_token")
```

The client retains no token result, persists no credentials, coordinates no
refreshes and never refreshes on 401. Save the complete refresh response before
using it, including any replacement refresh token.
There is **one HTTP attempt per API or OAuth call**, apart from streaming download
redirects. If a refresh times
out, has an interrupted response or a server failure, its outcome is uncertain;
do not replay the old refresh token. A malformed token response is also unsafe to
replay. Handle `invalid_grant` by asking the user to sign in again. Revocation
sends the token and optional type hint to the server and accepts an empty 200
response.

## Errors and caller decisions

```python
from foldr_client import FoldrError, OAuthError, StreamError, TransportError

try:
    page = client.search("forecast")
except FoldrError as error:
    print(error.status_code, error.code, error.retry_after)
    # error.problem retains type/detail/instance and provider extensions.
    # Decide whether/when to retry; honor Retry-After for 429 and 503.
```

All client exceptions derive from `FoldrClientError`:

| Error | Information |
| --- | --- |
| `FoldrError` | `status_code`, `headers`, full parsed `problem`, `type`, `code`, `detail`, `instance`, raw `retry_after`, capped `body`, `body_truncated` |
| `OAuthError` | Above, plus OAuth `error` and `outcome_uncertain` |
| `TransportError` | `operation`, `outcome_uncertain`; true for mutation/token request network failures |
| `StreamError` | `bytes_received` actually yielded before interruption; no replay |
| `ProtocolError` | Unexpected success body, malformed callback, upload length mismatch or unsafe redirect; `outcome_uncertain=True` when a mutation may have started |

HTML rate-limit responses and the disabled-API JSON 404 are retained without
assuming an RFC 9457 body. Error bodies are capped at 64 KiB so a failed download
cannot force whole-file buffering. Compressed error bodies remain capped wire
bytes rather than being decompressed. Exception messages omit provider text and
URLs; error attributes and headers may contain sensitive data, so redact before logging.
Failures during an error body's transfer may surface as transport errors.

Successful JSON responses are limited to 8 MiB for `FoldrClient`/`FoldrSite` and
64 KiB for `FoldrOAuth`. Set the positive integer `max_json_bytes` constructor
option to adjust a limit. Unexpected compression is rejected before decoding;
oversized responses raise `ProtocolError` and close without retry. Failed
mutation/token responses retain `outcome_uncertain=True`. File streams are not
subject to the JSON limit. See [configuration examples](docs/client-safety.md).

There are no implicit retries, sleeps, snapshot restarts or permission checks.
Foldr's 401/403/404 and storage connection errors reach the caller unchanged.
Use structured type/code/status rather than matching human-readable detail.

## Development and verification

See [development instructions](docs/development.md) for setup, tests and isolated
package installation. [API reference notes](docs/api-reference.md) map supported
operations to their client methods and routes. Runnable examples are in
[examples](examples).
See [writing and upload examples](docs/writes.md) for ETags, idempotency keys,
operations, sharing and credential-isolated direct uploads, and the reference
inventory for HEAD and every method signature.

Tests simulate HTTP, pagination, partial results, errors, ranges, exports, streams,
OAuth and credential isolation using synthetic data and mock transports. The
request-contract tests check the local request definitions for all 29 operations.
These tests check the implementation; they do not establish compatibility with a
live Foldr deployment. Async I/O and storage-specific or administration endpoints
are outside this package's scope.

## Contributing and security

See [contribution guidance](CONTRIBUTING.md) for bug reports, feature requests and
pull requests. Report suspected vulnerabilities using the
[private security reporting guidance](SECURITY.md).

## License

This project is available under the [MIT License](LICENSE).
