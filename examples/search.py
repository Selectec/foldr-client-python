"""Report each search page, including its incomplete-result information."""

import argparse
import os

from foldr_client import FoldrClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    args = parser.parse_args()
    with FoldrClient(
        os.environ["FOLDR_SITE"],
        access_token=os.environ["FOLDR_ACCESS_TOKEN"],
    ) as client:
        for page in client.iter_search_pages(args.query):
            print("Estimate:", page.total_estimate, "Not searched:", page.not_searched)
            print("Truncated:", page.truncated)
            for hit in page.items:
                print(hit["item"]["share_id"], hit["item"]["path"])


if __name__ == "__main__":
    main()
