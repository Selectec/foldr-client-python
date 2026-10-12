"""Create a Foldr file with a caller-owned request key and bounded streaming."""

import argparse
import os
from pathlib import Path

from foldr_client import FoldrClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("share_id")
    parser.add_argument("path", help="Destination path inside the share")
    parser.add_argument("source", type=Path, help="Existing local file")
    parser.add_argument(
        "--idempotency-key", required=True, help="Save and reuse for this request only"
    )
    args = parser.parse_args()
    with (
        FoldrClient(
            os.environ["FOLDR_SITE"],
            access_token=os.environ["FOLDR_ACCESS_TOKEN"],
        ) as client,
        args.source.open("rb") as source,
    ):
        response = client.upload_file(
            args.share_id,
            args.path,
            source,
            content_length=os.fstat(source.fileno()).st_size,
            idempotency_key=args.idempotency_key,
        )
        print(response.status_code, response.data["path"], response.etag)


if __name__ == "__main__":
    main()
