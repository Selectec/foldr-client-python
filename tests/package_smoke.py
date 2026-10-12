"""Run against an installed wheel from outside the checkout (no live HTTP)."""

from importlib.metadata import version

import httpx

from foldr_client import ApiResponse, DirectUpload, FoldrClient, FoldrOAuth, FoldrSite


def main():
    assert version("foldr-client") == "0.1.0"
    with FoldrClient(
        "https://files.example.com",
        access_token="synthetic-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"name": "Alex"})),
    ) as client:
        assert client.me()["name"] == "Alex"
    with FoldrOAuth("https://files.example.com", client_id="synthetic-client") as oauth:
        context = oauth.authorize("http://localhost:8000/callback", scopes=["foldr.read"])
        assert "code_challenge_method=S256" in context.url
    with FoldrSite(
        "https://files.example.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"paths": {}})),
    ) as site:
        assert isinstance(site.openapi(), ApiResponse)
    with FoldrClient(
        "https://files.example.com",
        access_token="synthetic-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                201,
                json={"path": "/Canonical"},
                headers={"ETag": '"v1"'},
            )
        ),
    ) as client:
        created = client.create_folder("share", "/canonical", idempotency_key="caller-key")
        assert created.data["path"] == "/Canonical" and created.etag == '"v1"'
    with DirectUpload(
        {
            "url": "https://storage.example.com/upload",
            "method": "PUT",
            "headers": {},
            "size": 1,
            "chunk_size": None,
        },
        transport=httpx.MockTransport(lambda request: httpx.Response(201)),
    ) as storage:
        assert storage.send(b"x", content_length=1).status_code == 201
    print("Installed Foldr client smoke check passed")


if __name__ == "__main__":
    main()
