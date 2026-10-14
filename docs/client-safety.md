# Client safety configuration

The defaults require HTTPS, verify TLS, isolate credentials, and avoid automatic
retries. The settings below control JSON memory use, HTTP dependency logs, and
which storage destinations an application will contact.

## JSON response limits

| Client | Default maximum successful JSON body |
| --- | --- |
| `FoldrClient` | 8 MiB (8,388,608 bytes) |
| `FoldrSite` (including OpenAPI and registration) | 8 MiB |
| `FoldrOAuth` | 64 KiB (65,536 bytes) |

Set `max_json_bytes` to a positive integer on the relevant constructor when an
expected response needs a different bound:

```python
from foldr_client import FoldrClient, FoldrOAuth, FoldrSite

with FoldrClient(site, access_token=token, max_json_bytes=4 * 1024 * 1024) as client:
    account = client.me()

with FoldrSite(site, max_json_bytes=16 * 1024 * 1024) as public:
    contract = public.openapi().data

with FoldrOAuth(site, client_id=client_id, max_json_bytes=128 * 1024) as oauth:
    authorization = oauth.authorize(callback_uri, scopes=["foldr.read"])
```

The limit counts actual serialized JSON bytes, not Content-Length. It bounds the
body retained for parsing; the parsed Python objects consume additional memory.
Unexpected compression is rejected before reading/decompression, even if the
compressed body is small. Oversize or malformed JSON raises `ProtocolError`,
closes the response and does not retry. On a mutation or token exchange,
`outcome_uncertain=True` means the request may already have taken effect: reconcile
the result rather than blindly replaying it.

File downloads continue to stream without this JSON cap. Error bodies keep their
separate 64 KiB wire-byte cap. HEAD, 204 and 304 do not consume response bodies.
Custom transports are responsible for any buffering they perform before returning
a response; the client cannot prevent a transport from preloading a whole body.

## Keep signed URLs out of dependency logs

HTTPX INFO records contain complete request URLs. HTTPCore DEBUG records can
include headers and other sensitive details. Signed storage credentials can live
in URL paths as well as query strings, so removing a query string alone is not
sufficient. Safe exception messages do not sanitize these dependency logs.

`HTTPLogFilter` suppresses all records from `httpx`, `httpcore` and their child
loggers. Unrelated application logs remain visible. For a new application's
startup logging configuration:

```python
import logging
from foldr_client import HTTPLogFilter

output = logging.StreamHandler()
output.addFilter(HTTPLogFilter())
logging.basicConfig(level=logging.INFO, handlers=[output])
```

If logging is already configured, `basicConfig` may do nothing: add the filter to
each existing output handler instead. This includes handlers attached directly to
dependency loggers, file handlers, telemetry handlers and queue handlers. Apply
the filter before records leave the process. A filter on the root logger alone
does not filter records propagated from descendant loggers.

Importing or constructing the filter makes no global logging changes. Suppression
also removes HTTP dependency error diagnostics; SDK exceptions still reach the
caller. The filter does not redact application logs, custom transport logs,
external tracing, or printed response data. Never log tokens, authorization
contexts, upload sessions, signed URLs or raw sensitive error attributes yourself.

## Restrict storage destinations when needed

Both `FoldrClient` and `DirectUpload` accept `destination_policy`. It receives an
`httpx.URL` and must return the literal boolean `True` to approve a destination.
It runs for every download redirect, including subsequent and same-origin hops,
and before every direct-upload send, including each chunk. The existing HTTPS and
no-URL-credentials checks still apply.

For an application with a known storage host:

```python
import httpx
from foldr_client import DirectUpload, FoldrClient


def approved_storage(url: httpx.URL) -> bool:
    return url.host == "storage.example.com" and url.port in (None, 443)


with FoldrClient(site, access_token=token, destination_policy=approved_storage) as client:
    with client.stream_content(share_id, path) as stream:
        for chunk in stream.iter_bytes():
            consume(chunk)

with DirectUpload(upload_session, destination_policy=approved_storage) as storage:
    storage.send(body, content_length=upload_session["size"])
```

Use the hosts appropriate to your storage, including any legitimate CDN hops.
The callback sees the full sensitive URL: do not log it. Rejection, a non-boolean
approval, or a callback exception raises a generic `ProtocolError` before a
request to that destination, without consuming upload bytes. Earlier upload
chunks or requests may already have succeeded. Callback exceptions are suppressed
from normal formatted tracebacks so they cannot echo the signed URL.

The policy is opt-in. Without it, HTTPS destinations including private storage
continue to work. It does not control the initial configured Foldr site, perform
DNS resolution, or prevent DNS rebinding. An application accepting untrusted
sites must validate them separately. Deployments requiring network isolation
also need appropriate egress controls; URL hostname checks are not a firewall.

## Revocation and version guarantees

`stream_content(..., direct=False)` requests Foldr-proxied bytes, allowing Foldr
to check access on each new request. It does not revoke previously issued storage
URLs, which can remain valid after an app disconnect or permission change. Direct
URL validity is backend-dependent; the API's `expires_at` is not a guaranteed
maximum lifetime. Neither mode can retract bytes already downloaded.

The SDK preserves server ETags unchanged. Their concurrency guarantees depend on
the storage backend; weak SMB size/mtime tags are not strong content-version
identifiers. See [request keys and ETags](writes.md#request-keys-and-etags).
