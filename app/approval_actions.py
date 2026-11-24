"""Sign a human-entered Notion approval before publishing."""
from __future__ import annotations

from typing import Any, Iterable

from app.approval import (
    ApprovalValidationError,
    compute_snapshot_signature,
    validate_approval_page,
)


def sign_existing_approval(
    notion_client: Any,
    page_id: str,
    authorized_approver_ids: Iterable[str],
    snapshot_secret: str,
    *,
    is_founder_content: bool = False,
    authorized_founder_approver_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Sign the exact approved Notion state without changing its approver fields."""
    page = notion_client.retrieve_page(page_id)
    if (
        not isinstance(page, dict)
        or page.get("object") != "page"
        or page.get("in_trash", False)
    ):
        raise ApprovalValidationError("Target Notion page is unavailable or in Trash.")

    properties = page.get("properties") or {}
    signature = compute_snapshot_signature(properties, snapshot_secret)
    notion_client.update_page_properties(
        page_id,
        {
            "Approval Snapshot Hash": {
                "rich_text": [
                    {"type": "text", "text": {"content": signature}}
                ]
            }
        },
    )

    signed_page = notion_client.retrieve_page(page_id)
    result = validate_approval_page(
        signed_page,
        authorized_approver_ids,
        snapshot_secret,
        is_founder_content=is_founder_content,
        authorized_founder_approver_ids=authorized_founder_approver_ids,
    )
    if not result.valid:
        raise ApprovalValidationError(
            "Notion approval could not be signed and verified: "
            + "; ".join(result.errors)
        )
    return signed_page
