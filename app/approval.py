"""Small, fail-closed Notion approval and content-snapshot validation.

Notion remains the source of truth. Every publish decision must validate a
freshly retrieved page. The webhook is only a notification and is never
accepted as approval evidence.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


CONTENT_FIELDS = (
    "Title",
    "Content ID",
    "Source",
    "Caption",
    "Assets / Media URLs",
    "Target Platforms",
    "Scheduled Time",
)


@dataclass(frozen=True)
class ApprovalValidationResult:
    valid: bool
    errors: tuple[str, ...]
    page_id: str | None = None


class ApprovalValidationError(RuntimeError):
    """Raised when an approval action cannot safely be accepted."""


def _normalise_ids(values: Iterable[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _rich_text_value(properties: dict[str, Any], name: str, expected_type: str) -> tuple[str, str | None]:
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return "", f"Missing property: {name}"
    if prop.get("type") != expected_type:
        return "", f"Invalid property type for {name}; expected {expected_type}."
    if prop.get("has_more"):
        return "", f"Property {name} is paginated; retrieve its complete value before approval."

    chunks = prop.get(expected_type, [])
    if not isinstance(chunks, list):
        return "", f"Invalid value for {name}."

    text_parts: list[str] = []
    for item in chunks:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if plain is None:
            plain = item.get("text", {}).get("content", "")
        text_parts.append(str(plain))
    return "".join(text_parts), None


def _date_value(properties: dict[str, Any], name: str) -> tuple[dict[str, Any] | None, str | None]:
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return None, f"Missing property: {name}"
    if prop.get("type") != "date":
        return None, f"Invalid property type for {name}; expected date."
    value = prop.get("date")
    if value is not None and not isinstance(value, dict):
        return None, f"Invalid date value for {name}."
    return value, None


def _parse_aware_timestamp(value: Any, field_name: str) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str) or "T" not in value:
        return None, f"{field_name} must contain a date and time, not a date only."
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, f"{field_name} is not a valid ISO-8601 timestamp."
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, f"{field_name} must include a timezone or UTC offset."
    return parsed.astimezone(timezone.utc), None


def build_content_snapshot(properties: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a canonical snapshot of fields whose changes require re-approval."""
    errors: list[str] = []
    snapshot: dict[str, Any] = {}

    title, error = _rich_text_value(properties, "Title", "title")
    if error:
        errors.append(error)
    elif not title.strip():
        errors.append("Title is required before approval.")
    snapshot["Title"] = title

    for name in ("Content ID", "Source", "Caption", "Assets / Media URLs"):
        value, error = _rich_text_value(properties, name, "rich_text")
        if error:
            errors.append(error)
        elif name in ("Content ID", "Caption") and not value.strip():
            errors.append(f"{name} is required before approval.")
        snapshot[name] = value

    platforms_prop = properties.get("Target Platforms")
    if not isinstance(platforms_prop, dict):
        errors.append("Missing property: Target Platforms")
        platforms: list[str] = []
    elif platforms_prop.get("type") != "multi_select":
        errors.append("Invalid property type for Target Platforms; expected multi_select.")
        platforms = []
    else:
        selected = platforms_prop.get("multi_select", [])
        platforms = sorted(
            str(item.get("name", "")).strip()
            for item in selected
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        )
        if not platforms:
            errors.append("At least one target platform is required before approval.")
    snapshot["Target Platforms"] = platforms

    scheduled, error = _date_value(properties, "Scheduled Time")
    if error:
        errors.append(error)
    snapshot["Scheduled Time"] = scheduled

    if errors:
        return None, errors
    return snapshot, []


def compute_snapshot_signature(properties: dict[str, Any], secret: str) -> str:
    """Create a keyed snapshot signature; a plain hash could be forged by editors."""
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise ValueError("APPROVAL_SNAPSHOT_SECRET must be at least 32 bytes.")

    snapshot, errors = build_content_snapshot(properties)
    if errors or snapshot is None:
        raise ApprovalValidationError("Cannot sign incomplete content: " + "; ".join(errors))

    # Bind the signature to the approval decision as well as the content. This
    # prevents a Notion editor from changing the approver or approval time while
    # leaving a previously valid content signature in place.
    metadata, metadata_errors = _build_approval_metadata(properties)
    if metadata_errors or metadata is None:
        raise ApprovalValidationError(
            "Cannot sign incomplete approval metadata: " + "; ".join(metadata_errors)
        )

    signed_payload = {"content": snapshot, "approval": metadata}
    canonical = json.dumps(
        signed_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return f"hmac-sha256:v1:{digest}"


def _select_name(properties: dict[str, Any], name: str) -> tuple[str | None, str | None]:
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return None, f"Missing property: {name}"
    if prop.get("type") != "select":
        return None, f"Invalid property type for {name}; expected select."
    selected = prop.get("select")
    if selected is None:
        return None, None
    if not isinstance(selected, dict):
        return None, f"Invalid select value for {name}."
    return selected.get("name"), None


def _people_ids(properties: dict[str, Any], name: str) -> tuple[list[str], str | None]:
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return [], f"Missing property: {name}"
    if prop.get("type") != "people":
        return [], f"Invalid property type for {name}; expected people."
    if prop.get("has_more"):
        return [], f"Property {name} is paginated; retrieve its complete value before approval."

    people = prop.get("people", [])
    if not isinstance(people, list):
        return [], f"Invalid people value for {name}."
    return [str(person.get("id", "")).lower() for person in people if isinstance(person, dict) and person.get("id")], None


def _approval_time(properties: dict[str, Any], name: str) -> tuple[datetime | None, str | None]:
    value, error = _date_value(properties, name)
    if error:
        return None, error
    if not value or not value.get("start"):
        return None, f"{name} is missing."
    return _parse_aware_timestamp(value.get("start"), name)


def _build_approval_metadata(
    properties: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the mutable approval fields included in the HMAC signature."""
    errors: list[str] = []

    status, error = _select_name(properties, "Approval Status")
    if error:
        errors.append(error)

    approver_ids, error = _people_ids(properties, "Approver")
    if error:
        errors.append(error)
    elif len(approver_ids) != 1:
        errors.append("Exactly one approver is required to sign the approval metadata.")

    approved_at, error = _approval_time(properties, "Approval Timestamp")
    if error:
        errors.append(error)

    metadata: dict[str, Any] = {
        "approval_status": status,
        "approver_ids": sorted(approver_ids),
        "approval_timestamp_utc": approved_at.isoformat() if approved_at else None,
    }

    founder_required_prop = properties.get("Founder Approval Required")
    if founder_required_prop is not None:
        if (
            not isinstance(founder_required_prop, dict)
            or founder_required_prop.get("type") != "checkbox"
            or not isinstance(founder_required_prop.get("checkbox"), bool)
        ):
            errors.append("Founder Approval Required checkbox is invalid.")
        else:
            founder_required = founder_required_prop["checkbox"]
            founder_status, status_error = _select_name(
                properties, "Founder Approval Status"
            )
            founder_ids, people_error = _people_ids(
                properties, "Founder Approver"
            )
            founder_date, date_error = _date_value(
                properties, "Founder Approval Timestamp"
            )

            for item_error in (status_error, people_error, date_error):
                if item_error:
                    errors.append(item_error)

            founder_timestamp = None
            if founder_date and founder_date.get("start"):
                parsed, parse_error = _parse_aware_timestamp(
                    founder_date.get("start"), "Founder Approval Timestamp"
                )
                if parse_error:
                    errors.append(parse_error)
                elif parsed:
                    founder_timestamp = parsed.isoformat()

            metadata["founder_approval"] = {
                "required": founder_required,
                "status": founder_status,
                "approver_ids": sorted(founder_ids),
                "timestamp_utc": founder_timestamp,
            }

    if errors:
        return None, errors
    return metadata, []


def _now_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    return value.astimezone(timezone.utc)


def validate_approval_page(
    page: dict[str, Any],
    authorized_approver_ids: Iterable[str],
    snapshot_secret: str,
    *,
    is_founder_content: bool = False,
    authorized_founder_approver_ids: Iterable[str] = (),
    require_founder_approval: bool = True,
    now: datetime | None = None,
    future_clock_skew: timedelta = timedelta(minutes=2),
) -> ApprovalValidationResult:
    """Validate a retrieved Notion page. Never accept webhook fields as a page."""
    errors: list[str] = []
    page_id = page.get("id") if isinstance(page, dict) else None

    if not isinstance(page, dict) or page.get("object") != "page":
        return ApprovalValidationResult(False, ("A freshly retrieved Notion page is required.",), page_id)
    if page.get("in_trash", False):
        errors.append("Content page is in the Notion trash.")

    properties = page.get("properties")
    if not isinstance(properties, dict):
        return ApprovalValidationResult(False, ("Page properties are missing.",), page_id)

    status, error = _select_name(properties, "Approval Status")
    if error:
        errors.append(error)
    elif status != "Approved":
        errors.append("Content approval status is not Approved.")

    approver_ids, error = _people_ids(properties, "Approver")
    if error:
        errors.append(error)
    else:
        allowed = _normalise_ids(authorized_approver_ids)
        if len(approver_ids) != 1:
            errors.append("Exactly one authorized approver must be assigned.")
        elif not allowed or approver_ids[0] not in allowed:
            errors.append("Approver is not in the authorized approver allowlist.")

    approved_at, error = _approval_time(properties, "Approval Timestamp")
    if error:
        errors.append(error)
    elif approved_at and approved_at > _now_utc(now) + future_clock_skew:
        errors.append("Approval Timestamp is in the future.")

    snapshot, snapshot_errors = build_content_snapshot(properties)
    errors.extend(snapshot_errors)

    stored_signature, error = _rich_text_value(
        properties, "Approval Snapshot Hash", "rich_text"
    )
    if error:
        errors.append(error)
    elif not stored_signature:
        errors.append("Approval Snapshot Hash is missing; content has no trusted approval snapshot.")
    elif snapshot is not None:
        try:
            expected_signature = compute_snapshot_signature(properties, snapshot_secret)
        except (ValueError, ApprovalValidationError) as exc:
            errors.append(str(exc))
        else:
            if not hmac.compare_digest(stored_signature, expected_signature):
                errors.append("Approved content has changed since its approval snapshot was recorded.")

    if is_founder_content:
        founder_required_prop = properties.get("Founder Approval Required")
        if not isinstance(founder_required_prop, dict) or founder_required_prop.get("type") != "checkbox":
            errors.append("Founder Approval Required checkbox is missing or has the wrong type.")
        else:
            founder_required = founder_required_prop.get("checkbox")
            if not isinstance(founder_required, bool):
                errors.append("Founder Approval Required must have a boolean value.")
            elif founder_required and require_founder_approval:
                founder_status, error = _select_name(properties, "Founder Approval Status")
                if error:
                    errors.append(error)
                elif founder_status != "Approved":
                    errors.append("Founder-specific approval is required but not Approved.")

                founder_ids, error = _people_ids(properties, "Founder Approver")
                if error:
                    errors.append(error)
                else:
                    founder_allowed = _normalise_ids(authorized_founder_approver_ids)
                    if len(founder_ids) != 1:
                        errors.append("Exactly one founder approver must be assigned.")
                    elif not founder_allowed or founder_ids[0] not in founder_allowed:
                        errors.append("Founder approver is not in the authorized founder allowlist.")

                founder_at, error = _approval_time(properties, "Founder Approval Timestamp")
                if error:
                    errors.append(error)
                else:
                    if founder_at and founder_at > _now_utc(now) + future_clock_skew:
                        errors.append("Founder Approval Timestamp is in the future.")
                    if founder_at and approved_at and founder_at < approved_at:
                        errors.append("Founder approval cannot predate the primary approval.")

    return ApprovalValidationResult(not errors, tuple(errors), page_id)


def validate_latest_approval(
    notion_client: Any,
    page_id: str,
    authorized_approver_ids: Iterable[str],
    snapshot_secret: str,
    **kwargs: Any,
) -> ApprovalValidationResult:
    """Retrieve the current Notion page immediately before a publish decision."""
    latest_page = notion_client.retrieve_page(page_id)
    return validate_approval_page(
        latest_page,
        authorized_approver_ids,
        snapshot_secret,
        **kwargs,
    )
