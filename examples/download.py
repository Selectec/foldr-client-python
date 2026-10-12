"""Stream one Foldr item to a local file; credentials come from the caller's env."""

import argparse
import os

from foldr_client import FoldrClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("share_id")
    parser.add_argument("path", help="Path inside the share, not a storage URL")
    parser.add_argument("output", help="Local destination (overwritten)")
    args = parser.parse_args()
    with FoldrClient(
        os.environ["FOLDR_SITE"],
        access_token=os.environ["FOLDR_ACCESS_TOKEN"],
    ) as client:
        item = client.get_item(args.share_id, args.path)
        with client.stream_content(args.share_id, item["path"]) as stream:
            with open(args.output, "wb") as output:
                for chunk in stream.iter_bytes():
                    output.write(chunk)


if __name__ == "__main__":
    main()
