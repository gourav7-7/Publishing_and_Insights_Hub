from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.approval import compute_snapshot_signature, validate_approval_page
from app.approval_actions import sign_existing_approval

SECRET = "test-only-snapshot-secret-with-more-than-32-bytes"
APPROVER = "11111111-1111-4111-8111-111111111111"
FOUNDER = "22222222-2222-4222-8222-222222222222"
UNAUTHORISED = "33333333-3333-4333-8333-333333333333"


def rich_text(value: str) -> dict:
    return {
        "type": "rich_text",
        "rich_text": ([{"type": "text", "plain_text": value}] if value else []),
    }


def build_page(*, founder_required: bool = False, founder_approved: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    properties = {
        "Title": {
            "type": "title",
            "title": [{"type": "text", "plain_text": "December launch"}],
        },
        "Content ID": rich_text("content-001"),
        "Source": rich_text("Editorial calendar"),
        "Caption": rich_text("A launch announcement for our new product."),
        "Assets / Media URLs": rich_text(""),
        "Target Platforms": {
            "type": "multi_select",
            "multi_select": [{"name": "LinkedIn"}],
        },
        "Scheduled Time": {
            "type": "date",
            "date": {"start": (now + timedelta(days=3)).isoformat()},
        },
        "Approval Status": {"type": "select", "select": {"name": "Approved"}},
        "Approver": {"type": "people", "people": [{"id": APPROVER}]},
        "Approval Timestamp": {
            "type": "date",
            "date": {"start": (now - timedelta(minutes=10)).isoformat()},
        },
        "Approval Snapshot Hash": rich_text(""),
        "Founder Approval Required": {"type": "checkbox", "checkbox": founder_required},
        "Founder Approval Status": {
            "type": "select",
            "select": {"name": "Approved" if founder_approved else "Pending"},
        },
        "Founder Approver": {
            "type": "people",
            "people": ([{"id": FOUNDER}] if founder_approved else []),
        },
        "Founder Approval Timestamp": {
            "type": "date",
            "date": (
                {"start": (now - timedelta(minutes=5)).isoformat()}
                if founder_approved else None
            ),
        },
        "Publishing Status": {"type": "select", "select": {"name": "Ready"}},
    }
    properties["Approval Snapshot Hash"] = rich_text(
        compute_snapshot_signature(properties, SECRET)
    )
    return {
        "object": "page",
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "in_trash": False,
        "properties": properties,
    }


class ApprovalTests(unittest.TestCase):
    def test_authorized_approval_with_matching_signature_is_valid(self):
        result = validate_approval_page(
            build_page(),
            (APPROVER,),
            SECRET,
        )
        self.assertTrue(result.valid, result.errors)

    def test_unapproved_content_is_rejected(self):
        page = build_page()
        page["properties"]["Approval Status"]["select"]["name"] = "Pending"
        page["properties"]["Approval Snapshot Hash"] = rich_text(
            compute_snapshot_signature(page["properties"], SECRET)
        )
        result = validate_approval_page(page, (APPROVER,), SECRET)
        self.assertFalse(result.valid)
        self.assertTrue(any("not Approved" in error for error in result.errors))

    def test_unauthorized_approver_is_rejected(self):
        page = build_page()
        page["properties"]["Approver"]["people"] = [{"id": UNAUTHORISED}]
        page["properties"]["Approval Snapshot Hash"] = rich_text(
            compute_snapshot_signature(page["properties"], SECRET)
        )
        result = validate_approval_page(page, (APPROVER,), SECRET)
        self.assertFalse(result.valid)
        self.assertTrue(any("allowlist" in error for error in result.errors))

    def test_caption_change_invalidates_signed_snapshot(self):
        page = build_page()
        page["properties"]["Caption"] = rich_text("Changed after approval")
        result = validate_approval_page(page, (APPROVER,), SECRET)
        self.assertFalse(result.valid)
        self.assertTrue(any("changed since" in error for error in result.errors))

    def test_founder_content_requires_founder_signoff_when_flagged(self):
        page = build_page(founder_required=True, founder_approved=False)
        result = validate_approval_page(
            page,
            (APPROVER,),
            SECRET,
            is_founder_content=True,
            authorized_founder_approver_ids=(FOUNDER,),
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("Founder-specific approval" in error for error in result.errors))

    def test_valid_founder_approval_is_accepted(self):
        page = build_page(founder_required=True, founder_approved=True)
        result = validate_approval_page(
            page,
            (APPROVER,),
            SECRET,
            is_founder_content=True,
            authorized_founder_approver_ids=(FOUNDER,),
        )
        self.assertTrue(result.valid, result.errors)

    def test_sign_existing_approval_does_not_change_approver(self):
        page = build_page()
        notion = Mock()
        notion.retrieve_page.side_effect = [copy.deepcopy(page), copy.deepcopy(page)]

        def update_page_properties(page_id, update):
            signed_value = update["Approval Snapshot Hash"]
            page["properties"]["Approval Snapshot Hash"] = {
                "type": "rich_text",
                **signed_value,
            }
            notion.retrieve_page.side_effect = [copy.deepcopy(page)]

        notion.update_page_properties.side_effect = update_page_properties
        signed = sign_existing_approval(
            notion,
            page["id"],
            (APPROVER,),
            SECRET,
        )
        self.assertTrue(signed["properties"]["Approval Snapshot Hash"]["rich_text"])
        self.assertEqual(
            signed["properties"]["Approver"]["people"][0]["id"],
            APPROVER,
        )


if __name__ == "__main__":
    unittest.main()
