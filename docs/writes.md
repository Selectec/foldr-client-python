# Writes, operations and direct uploads

These examples use synthetic file paths, names and email addresses. They assume
an open `FoldrClient` named `client`, a caller-selected `share_id`, and an account
with permission to make each change. Run each example independently with test
files. The client sends the request; the server decides whether to allow it.

## Request keys and ETags

Save an idempotency key with the intended request before sending it. The client
requires keys for methods that take `idempotency_key` and never generates or
replaces them for you. Retain the same key when reconciling an uncertain result.

```python
from uuid import uuid4

request_key = str(uuid4())  # persist alongside this request before sending
created = client.create_folder(
    share_id,
    "/Examples/New folder",
    idempotency_key=request_key,
)

renamed = client.rename_item(
    share_id,
    created.data["path"],
    "Renamed folder",
    if_match=created.etag,
)
```

Methods with `if_match` send the caller's ETag as `If-Match`. Read fresh metadata
when you need the current ETag. Overwrite, restore, move and delete reject `*`
locally because these methods require an explicit version. Other validation and
conflict handling belong to the server. A failed request is raised to the caller;
the client does not force the change or retry it.

Concurrency protection depends on the storage backend. Foldr can return weak
size/mtime ETags for SMB, where different contents of the same size within the
timestamp resolution may share a tag. The client preserves the tag, including
`W/`, exactly as the API requires; it does not turn it into a strong content
version or guarantee atomic server-side checks. Do not remove `W/` or substitute
a locally invented tag. Workflows needing stronger assurance must use server
and storage controls that provide it.

## Upload and overwrite

```python
from pathlib import Path
from uuid import uuid4

source = Path("example.txt")
request_key = str(uuid4())  # save before sending
with source.open("rb") as body:
    uploaded = client.upload_file(
        share_id,
        "/Examples/example.txt",
        body,
        content_length=source.stat().st_size,
        idempotency_key=request_key,
    )

# An independent overwrite example: read current metadata before changing it.
current = client.get_item_response(share_id, "/Examples/example.txt")
with source.open("rb") as body:
    updated = client.overwrite_file(
        share_id,
        current.data["path"],
        body,
        content_length=source.stat().st_size,
        if_match=current.etag,
    )
```

`upload_file` sends raw bytes with `If-None-Match: *`, `Idempotency-Key` and
`Content-Length`. `overwrite_file` uses `If-Match`. Both accept bytes, a binary
file, or an iterable of byte chunks. File reads begin at the current position and
stop at `content_length`; the client leaves caller-owned files open. A short file
or an iterable length mismatch raises `ProtocolError`. A byte-string length
mismatch raises `ValueError` before the request is sent.

The client buffers at most 64 KiB of each upload piece. Keep the source stable
while uploading. After a transport failure, inspect `outcome_uncertain` and
reconcile the server's state before deciding what to do next. The client does not
resume, retry or undo an upload.

## Copy, move, restore and delete

The methods below return `ApiResponse`, so inspect both status and data. A pending
response needs separate follow-up; receiving a response does not always mean the
change has finished.

```python
from uuid import uuid4

copied = client.copy_item(
    share_id,
    "/Examples/example.txt",
    to_share=archive_share_id,
    to_path="/Archive/example.txt",
    idempotency_key=str(uuid4()),  # save before sending
    respond_async=True,
)

current = client.get_item_response(share_id, "/Examples/example.txt")
moved = client.move_item(
    share_id,
    current.data["path"],
    to_path="/Archive/example.txt",
    if_match=current.etag,
    idempotency_key=str(uuid4()),  # save before sending
)
```

`restore_version` takes a caller-selected `version_id`, `if_match` and
`idempotency_key`. `delete_item` takes `if_match` and `idempotency_key`. Use current
metadata and make the user's intent explicit before either action. The client
returns deletion/recovery information supplied by the server but provides no
undelete method.

## Pending operations

`get_operation(id)` returns `ApiResponse` and accepts 200 or 202. The client does
not schedule polling. A caller can use the response's `retry_after` value to
schedule a later check:

```python
if copied.status_code == 202:
    operation_id = copied.data["id"]
    # Persist this identifier and schedule a later check using copied.retry_after.
    progress = client.get_operation(operation_id)
    print(progress.status_code)
```

`list_operations` and `iter_operation_pages` retrieve operation pages.
`cancel_operation(id)` sends a cancellation request. Interpret the returned state
and any server error before deciding whether another action is appropriate.
Sharing requests use their own methods and identifiers rather than operation
polling.

## Fields and sharing requests

```python
from uuid import uuid4

fields = client.list_fields().data
values = client.get_field_values(share_id, "/Examples/example.txt")
changed = client.set_field_value(
    share_id,
    "/Examples/example.txt",
    field_id,
    "Example value",
    if_match=values.etag,
)

request = client.create_share_request(
    share_id,
    "/Examples/example.txt",
    kind="secure_link",
    recipient_emails=["reader@example.com"],
    expiry_days=14,
    message="Example sharing request",
    idempotency_key=str(uuid4()),  # save before sending
)
state = client.get_share_request(request.data["id"])
```

Field values and response extensions are retained as returned. Sharing options
are sent to the server for validation, including unknown future `kind` values.
Use `get_share_request`, `list_share_requests` or `iter_share_request_pages` to
follow sharing requests. The client provides no approval-decision method.

## Direct storage uploads

`create_upload_session` returns the server's session data. `DirectUpload` uses its
storage method, URL and headers in a separate HTTP connection. It receives no
Foldr access token. Complete the session explicitly after sending the bytes:

```python
from pathlib import Path
from foldr_client import DirectUpload

source = Path("example.bin")
session = client.create_upload_session(
    share_id,
    "/Examples/example.bin",
    size=source.stat().st_size,
    content_type="application/octet-stream",
).data

with DirectUpload(session) as storage, source.open("rb") as body:
    if session["chunk_size"] is None:
        storage.send(body, content_length=session["size"])
    else:
        start = 0
        while start < session["size"]:
            size = min(session["chunk_size"], session["size"] - start)
            end = start + size - 1
            storage.send(
                body,
                content_length=size,
                content_range=f"bytes {start}-{end}/{session['size']}",
            )
            start += size

completed = client.complete_upload_session(session["id"])
```

`DirectUpload.send` returns status and headers while discarding the storage
response body. It rejects redirects, sends no inherited cookies and makes no
retry or resume attempt. The session's explicit storage authorization headers
are retained. Keep the session URL and headers out of logs and treat them as
credentials. `cancel_upload_session(id)` provides explicit session cancellation;
the server determines what cancellation can remove.

Storage conditions such as a session-supplied `If-None-Match: *` are preserved
unchanged. A storage 412 is raised as `FoldrError`; the helper never removes a
condition or retries to force an upload. Create-only enforcement belongs to the
server/storage signature, not to a caller-controlled SDK. Configure
`destination_policy` and HTTP log filtering as described in
[client safety configuration](client-safety.md).
