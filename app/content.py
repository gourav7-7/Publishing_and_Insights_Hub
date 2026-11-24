"""Read and validate the publishable content stored in Notion."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.approval import validate_approval_page


class ContentValidationError(RuntimeError):
    pass


def _property(properties: dict[str, Any], name: str, expected: str) -> dict[str, Any]:
    value = properties.get(name)
    if not isinstance(value, dict) or value.get("type") != expected:
        raise ContentValidationError(f"Notion property '{name}' is missing or must be {expected}.")
    return value


def _rich_text(properties: dict[str, Any], name: str) -> str:
    chunks = _property(properties, name, "rich_text").get("rich_text") or []
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in chunks if isinstance(item, dict)
    ).strip()


def _select(properties: dict[str, Any], name: str) -> str | None:
    selected = _property(properties, name, "select").get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _multi_select(properties: dict[str, Any], name: str) -> set[str]:
    values = _property(properties, name, "multi_select").get("multi_select") or []
    return {
        str(item.get("name", "")).strip().lower()
        for item in values if isinstance(item, dict) and item.get("name")
    }


def _date(properties: dict[str, Any], name: str) -> datetime:
    value = _property(properties, name, "date").get("date")
    raw = value.get("start") if isinstance(value, dict) else None
    if not raw:
        raise ContentValidationError(f"{name} must contain a date and time.")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContentValidationError(f"{name} must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContentValidationError(f"{name} must include a timezone or UTC offset.")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ApprovedContent:
    page_id: str
    content_type: str
    content_id: str
    caption: str
    scheduled_at: datetime
    snapshot_hash: str
    notion_status: str
    approval_details: dict[str, Any]


class NotionContentReader:
    def __init__(
        self,
        notion_client,
        *,
        platform: str,
        authorized_approver_ids: tuple[str, ...],
        authorized_founder_approver_ids: tuple[str, ...],
        approval_snapshot_secret: str,
    ):
        self.notion = notion_client
        self.platform = platform.lower()
        self.approver_ids = authorized_approver_ids
        self.founder_approver_ids = authorized_founder_approver_ids
        self.snapshot_secret = approval_snapshot_secret

    def read(self, page_id: str, content_type: str) -> ApprovedContent:
        if content_type not in {"brand", "founder"}:
            raise ContentValidationError("content_type must be 'brand' or 'founder'.")

        page = self.notion.retrieve_page(page_id)
        if (
            not isinstance(page, dict)
            or page.get("object") != "page"
            or page.get("in_trash", False)
        ):
            raise ContentValidationError("The Notion page is unavailable or in Trash.")

        validation = validate_approval_page(
            page,
            self.approver_ids,
            self.snapshot_secret,
            is_founder_content=(content_type == "founder"),
            authorized_founder_approver_ids=self.founder_approver_ids,
        )
        if not validation.valid:
            raise ContentValidationError(
                "Notion approval validation failed: " + "; ".join(validation.errors)
            )

        properties = page.get("properties") or {}
        status = _select(properties, "Publishing Status")
        if status not in {"Ready", "Scheduled", "Published", "Needs Reconciliation"}:
            raise ContentValidationError(
                f"Publishing Status must be Ready before submission; current status is {status!r}."
            )

        if _multi_select(properties, "Target Platforms") != {self.platform}:
            raise ContentValidationError(
                f"Select exactly the configured platform '{self.platform}' in Target Platforms."
            )

        caption = _rich_text(properties, "Caption")
        if not caption:
            raise ContentValidationError("Caption is empty.")
        if _rich_text(properties, "Assets / Media URLs"):
            raise ContentValidationError(
                "This take-home demo is text-only. Clear Assets / Media URLs before scheduling."
            )

        scheduled_at = _date(properties, "Scheduled Time")
        snapshot_hash = _rich_text(properties, "Approval Snapshot Hash")
        if not snapshot_hash:
            raise ContentValidationError("Approval Snapshot Hash is missing; approval is not signed.")
        if status == "Ready" and scheduled_at <= datetime.now(timezone.utc):
            raise ContentValidationError("Scheduled Time must be in the future.")

        people = _property(properties, "Approver", "people").get("people") or []
        approval_date = _property(properties, "Approval Timestamp", "date").get("date") or {}
        approval_details: dict[str, Any] = {
            "approval_status": _select(properties, "Approval Status"),
            "approver_ids": [p.get("id") for p in people if isinstance(p, dict)],
            "approval_timestamp": approval_date.get("start"),
        }
        if content_type == "founder":
            founder_people = properties.get("Founder Approver", {}).get("people", [])
            founder_date = properties.get("Founder Approval Timestamp", {}).get("date") or {}
            approval_details["founder_approver_ids"] = [
                p.get("id") for p in founder_people if isinstance(p, dict)
            ]
            approval_details["founder_approval_timestamp"] = founder_date.get("start")

        return ApprovedContent(
            page_id=page_id,
            content_type=content_type,
            content_id=_rich_text(properties, "Content ID") or page_id,
            caption=caption,
            scheduled_at=scheduled_at,
            snapshot_hash=snapshot_hash,
            notion_status=status,
            approval_details=approval_details,
        )
