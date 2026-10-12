# Foldr API v1 operation inventory

This inventory describes the 29 authenticated operations implemented by
`FoldrClient`. Each call uses the route shown below; the client does not load a
schema to decide which request to send.

Paths below are relative to the site's `/api/v1`. Resource IDs are encoded as
single path segments; item paths always go in query values or JSON.

| operationId | Python method | HTTP route | Result |
| --- | --- | --- | --- |
| getMe | `me()` | GET `/me` | dict |
| listShares | `list_shares(*, cursor=None, limit=None)` | GET `/shares` | Page |
| getShare | `get_share(share_id)` | GET `/shares/{share_id}` | dict |
| listChildren | `list_children(share_id, path="/", *, cursor=None, limit=None)` | GET `/shares/{share_id}/children` | Page |
| getItem | `get_item(share_id, path)` or `get_item_response(share_id, path, *, if_none_match=None)` | GET `/shares/{share_id}/item` | dict or ApiResponse |
| getContent | `stream_content(share_id, path, *, byte_range=None, direct=None, if_none_match=None)` | GET `/shares/{share_id}/content` | content context manager |
| getDownloadUrl | `get_download_url(share_id, path)` | GET `/shares/{share_id}/download-url` | ApiResponse |
| search | `search(q, *, share_id=None, cursor=None, limit=None)` | GET `/search` | Page |
| listFields | `list_fields()` | GET `/fields` | ApiResponse with array |
| getFieldValues | `get_field_values(share_id, path, *, if_none_match=None)` | GET `/shares/{share_id}/field-values` | ApiResponse with array or 304 |
| listVersions | `list_versions(share_id, path)` | GET `/shares/{share_id}/versions` | Page, all versions at once |
| createFolder | `create_folder(share_id, path, *, idempotency_key, on_conflict=None)` | POST `/shares/{share_id}/folders` | ApiResponse 201 |
| uploadFile | `upload_file(share_id, path, content, *, content_length, idempotency_key, on_conflict=None)` | PUT `/shares/{share_id}/files` | ApiResponse 201 |
| createUploadSession | `create_upload_session(share_id, path, *, size, content_type=None, on_conflict=None)` | POST `/uploads` | ApiResponse 201 |
| completeUploadSession | `complete_upload_session(session_id)` | POST `/uploads/{id}/complete` | ApiResponse 201 |
| cancelUploadSession | `cancel_upload_session(session_id)` | POST `/uploads/{id}/cancel` | ApiResponse 204 |
| renameItem | `rename_item(share_id, path, new_name, *, if_match)` | POST `/shares/{share_id}/rename` | ApiResponse 200 |
| copyItem | `copy_item(share_id, path, *, to_share, to_path, idempotency_key, on_conflict=None, respond_async=False)` | POST `/shares/{share_id}/copy` | ApiResponse 201/202 |
| overwriteFile | `overwrite_file(share_id, path, content, *, content_length, if_match)` | PUT `/shares/{share_id}/content` | ApiResponse 200 |
| restoreVersion | `restore_version(share_id, path, version_id, *, if_match, idempotency_key)` | POST `/shares/{share_id}/versions/restore` | ApiResponse 200/202 |
| moveItem | `move_item(share_id, path, *, to_path, if_match, idempotency_key, to_share=None, on_conflict=None, respond_async=False)` | POST `/shares/{share_id}/move` | ApiResponse 200/202 |
| deleteItem | `delete_item(share_id, path, *, if_match, idempotency_key)` | DELETE `/shares/{share_id}/items` | ApiResponse 200/202 |
| setFieldValue | `set_field_value(share_id, path, field_id, value, *, if_match)` | PUT `/shares/{share_id}/field-values/{field_id}` | ApiResponse with array |
| createShareRequest | `create_share_request(share_id, path, *, kind, idempotency_key, people=None, permission=None, recipient_emails=None, expiry_days=None, message=None)` | POST `/shares/{share_id}/share-requests` | ApiResponse 202 ShareRequest |
| getShareRequest | `get_share_request(request_id)` | GET `/share-requests/{id}` | ApiResponse |
| listShareRequests | `list_share_requests(*, cursor=None, limit=None)` | GET `/share-requests` | Page |
| getOperation | `get_operation(operation_id)` | GET `/operations/{id}` | ApiResponse 200/202 or terminal error |
| listOperations | `list_operations(*, cursor=None, limit=None)` | GET `/operations` | Page |
| cancelOperation | `cancel_operation(operation_id)` | POST `/operations/{id}/cancel` | ApiResponse 204 |

Page iterators: `iter_share_pages`, `iter_children_pages`, `iter_search_pages`,
`iter_operation_pages`, `iter_share_request_pages`. They accept the same arguments
as their single-page counterparts and yield every page, preserving completeness
and unchanged query parameters. Omitted limits are left to the server; supplied
limits are sent as query values. Fields and field values are plain arrays, not
pages. Versions are a single Page with no cursor parameter.

## HEAD and conditional reads

Every authenticated GET in the inventory supports:

```python
response = client.head(
    "getContent",
    share_id=share_id,
    params={"path": path, "direct": "false"},
)
```

`head(operation, *, share_id=None, identifier=None, params=None, if_none_match=None,
byte_range=None)` uses the documented operationId. Share reads require `share_id`;
`getOperation` and `getShareRequest` require `identifier`. Other query values use
`params`. It returns `ApiResponse(data=None, ...)`, retains status/headers and never
follows a content redirect or downloads bytes. It accepts getOperation's pending
202; `byte_range` supplies Range on a content HEAD.

`get_item_response`, `get_field_values` and `stream_content` support
If-None-Match behavior, preserving 304. OpenAPI has its own ETag/304 behavior.
ETags remain opaque, including weak markers and quotes.

## Site bootstrap and OAuth

`FoldrSite(site, *, timeout=60.0, transport=None)` owns a credential-free session:

| Method | Endpoint | Result |
| --- | --- | --- |
| `openapi(*, if_none_match=None, head=False)` | GET/HEAD `/api/v1/openapi.json` | ApiResponse 200/304 |
| `authorization_metadata(*, head=False)` | GET/HEAD `/.well-known/oauth-authorization-server` | ApiResponse |
| `protected_resource_metadata(*, head=False)` | GET/HEAD `/.well-known/oauth-protected-resource/api/v1` | ApiResponse |
| `problem_type(slug, *, head=False)` | GET/HEAD `/api/v1/problems/{slug}`, JSON | ApiResponse |
| `register(metadata)` | POST `/oauth/register`, JSON | ApiResponse 201 |

`FoldrOAuth(site, *, client_id, client_secret=None, auth_method="none", timeout=60.0,
transport=None)` provides these explicit operations:

| Method | Protocol |
| --- | --- |
| `authorize(redirect_uri, *, scopes)` | PKCE S256 browser URL `/oauth/authorize`; returns Authorization |
| `exchange_callback(callback_url, authorization)` | Validated state/issuer/target then POST `/oauth/token`; returns token dict |
| `refresh(refresh_token, *, scopes=None)` | One POST `/oauth/token`; returns rotated tokens for caller to save |
| `revoke(token, *, token_type_hint=None)` | POST `/oauth/revoke`; accepts empty 200, returns None |

`DirectUpload(session, *, timeout=60.0, transport=None)` sends bytes to the exact
storage method/URL/headers from createUploadSession. `send(content, *,
content_length, content_range=None)` returns status/headers with `data=None` and
does not interpret storage response bodies. Use a separate context from Foldr;
see [upload examples](writes.md). All four client classes own their optional
transport and support context management and `close()`.

## Request behavior and limits

Download `direct=None` omits the parameter and honors the share's setting;
`direct=False` requests bytes through Foldr. Upload-session `content_type=None`
omits that optional field. Optional query/body values set to `None` are generally
omitted, while `False`, zero and empty strings are preserved for server validation.

The package sends one request per API call and does not retry or poll operations
automatically. Streaming downloads may make additional requests to follow a
redirect. The server determines authorization, accepted parameters and available
storage features. This client exposes no direct version download, export-format
selection, approval-decision, undelete or registration-management method.

Tests use mock transports and synthetic responses. The request-contract tests
check these local route definitions; live deployment compatibility requires
separate verification with an authorized test account.

HTTP mechanics use [HTTPX streaming](https://www.python-httpx.org/quickstart/#streaming-responses)
and [transports](https://www.python-httpx.org/advanced/transports/). OAuth grant
construction uses [OAuthLib](https://oauthlib.readthedocs.io/en/latest/oauth2/clients/webapplicationclient.html).
