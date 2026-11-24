"""Sign a Notion approval after the human approver has updated its properties."""
from __future__ import annotations

import argparse

from app.approval_actions import sign_existing_approval
from app.config import get_settings
from app.notion_client import NotionClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--content-type", choices=("brand", "founder"), default="brand")
    args = parser.parse_args()

    settings = get_settings()
    notion = NotionClient(
        settings.notion_api_key,
        settings.notion_version,
        settings.notion_timeout_seconds,
    )
    page = sign_existing_approval(
        notion,
        args.page_id,
        settings.authorized_approver_ids,
        settings.approval_snapshot_secret,
        is_founder_content=args.content_type == "founder",
        authorized_founder_approver_ids=settings.authorized_founder_approver_ids,
    )
    print(f"Approval snapshot signed and verified for Notion page {page['id']}.")


if __name__ == "__main__":
    main()
