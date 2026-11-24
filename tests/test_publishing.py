from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.approval import compute_snapshot_signature
from app.buffer_client import BufferAuthenticationError, BufferOutcomeUnknown
from app.notion_client import NotionAPIError
from app.publishing import (
    PublishingConflict,
    PublishingError,
    PublishingOutcomeUnknown,
    PublishingService,
    _parse_stored_time,
)

SECRET = "publishing-test-secret-with-at-least-thirty-two-bytes"
APPROVER = "11111111-1111-4111-8111-111111111111"


def rt(value: str) -> dict:
    return {"type": "rich_text", "rich_text": ([{"type": "text", "plain_text": value}] if value else [])}


def make_page(
    *, caption: str = "Approved launch caption", status: str = "Ready", platform: str = "Threads"
) -> dict:
    now = datetime.now(timezone.utc)
    props = {
        "Title": {"type": "title", "title": [{"type": "text", "plain_text": "Launch"}]},
        "Content ID": rt("content-42"),
        "Source": rt("Editorial"),
        "Caption": rt(caption),
        "Assets / Media URLs": rt(""),
        "Target Platforms": {"type": "multi_select", "multi_select": [{"name": platform}]},
        "Scheduled Time": {"type": "date", "date": {"start": (now + timedelta(days=2)).isoformat()}},
        "Approval Status": {"type": "select", "select": {"name": "Approved"}},
        "Approver": {"type": "people", "people": [{"id": APPROVER}]},
        "Approval Timestamp": {"type": "date", "date": {"start": (now - timedelta(minutes=5)).isoformat()}},
        "Approval Snapshot Hash": rt(""),
        "Founder Approval Required": {"type": "checkbox", "checkbox": False},
        "Founder Approval Status": {"type": "select", "select": {"name": "Pending"}},
        "Founder Approver": {"type": "people", "people": []},
        "Founder Approval Timestamp": {"type": "date", "date": None},
        "Publishing Status": {"type": "select", "select": {"name": status}},
    }
    props["Approval Snapshot Hash"] = rt(compute_snapshot_signature(props, SECRET))
    return {"object": "page", "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "in_trash": False, "properties": props}


class PublishingServiceTests(unittest.TestCase):
    def setUp(self):
        self.page_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.page = make_page()
        self.notion = Mock()
        self.notion.retrieve_page.return_value = self.page
        self.buffer = Mock()
        self.database = Mock()
        self.slack = Mock()
        self.database.start_execution.return_value = "run-1"
        self.database.reserve_job.return_value = (True, {"id": "job-1", "status": "submitting"})
        self.due = datetime.fromisoformat(self.page["properties"]["Scheduled Time"]["date"]["start"])
        self.buffer.schedule_text_post.return_value = {
            "id": "buffer-post-1",
            "status": "scheduled",
            "dueAt": self.due.isoformat(),
        }
        self.service = PublishingService(
            notion_client=self.notion,
            buffer_client=self.buffer,
            database=self.database,
            slack=self.slack,
            channel_id="test-channel-1",
            platform="threads",
            authorized_approver_ids=(APPROVER,),
            authorized_founder_approver_ids=(),
            approval_snapshot_secret=SECRET,
        )

    def test_schedules_approved_item_and_writes_back_to_notion(self):
        result = self.service.publish(self.page_id, "brand")
        self.assertEqual(result.status, "scheduled")
        self.assertEqual(result.provider_post_id, "buffer-post-1")
        self.assertTrue(result.notion_updated)
        self.assertFalse(result.duplicate_request)
        self.buffer.schedule_text_post.assert_called_once()
        self.database.mark_scheduled.assert_called_once()
        self.database.finish_execution.assert_called_once()
        reserve = self.database.reserve_job.call_args.kwargs
        self.assertEqual(reserve["platform"], "threads")
        self.assertEqual(reserve["channel_id"], "test-channel-1")
        self.assertEqual(reserve["provider"], "Buffer")
        self.assertEqual(self.database.start_execution.call_args.kwargs["provider"], "Buffer")
        update = self.notion.update_page_properties.call_args.args[1]
        self.assertEqual(update["Provider"], {"select": {"name": "Buffer"}})
        self.assertEqual(update["Publishing Status"], {"select": {"name": "Scheduled"}})
        self.assertEqual(
            update["Provider Post ID"]["rich_text"][0]["text"]["content"],
            "buffer-post-1",
        )

    def test_stored_postgres_datetime_is_accepted_during_replay(self):
        self.database.reserve_job.return_value = (
            False,
            {
                "id": "job-1",
                "status": "scheduled",
                "provider_post_id": "buffer-post-1",
                "approval_snapshot_hash": self.page["properties"]["Approval Snapshot Hash"]["rich_text"][0]["plain_text"],
                "scheduled_at": self.due,
            },
        )
        self.buffer.get_post.return_value = {
            "id": "buffer-post-1",
            "status": "scheduled",
            "dueAt": self.due.isoformat(),
        }

        result = self.service.publish(self.page_id, "brand")

        self.assertTrue(result.duplicate_request)
        self.assertEqual(result.provider_post_id, "buffer-post-1")
        self.buffer.schedule_text_post.assert_not_called()
        self.buffer.get_post.assert_called_once_with("buffer-post-1")

    def test_stored_timestamp_parser_accepts_iso_and_datetime(self):
        self.assertEqual(_parse_stored_time(self.due), self.due.astimezone(timezone.utc))
        self.assertEqual(
            _parse_stored_time(self.due.isoformat().replace("+00:00", "Z")),
            self.due.astimezone(timezone.utc),
        )

    def test_replay_reuses_provider_id_without_second_mutation(self):
        self.database.reserve_job.return_value = (
            False,
            {
                "id": "job-1",
                "status": "scheduled",
                "provider_post_id": "buffer-post-1",
                "approval_snapshot_hash": self.page["properties"]["Approval Snapshot Hash"]["rich_text"][0]["plain_text"],
                "scheduled_at": self.due.isoformat().replace("+00:00", "Z"),
            },
        )
        self.buffer.get_post.return_value = {
            "id": "buffer-post-1", "status": "scheduled", "dueAt": self.due.isoformat()
        }
        result = self.service.publish(self.page_id, "brand")
        self.assertTrue(result.duplicate_request)
        self.assertEqual(result.provider_post_id, "buffer-post-1")
        self.buffer.schedule_text_post.assert_not_called()
        self.buffer.get_post.assert_called_once_with("buffer-post-1")

    def test_unknown_outcome_is_quarantined_and_alert_says_do_not_retry(self):
        self.buffer.schedule_text_post.side_effect = BufferOutcomeUnknown("timeout after submit")
        with self.assertRaises(PublishingOutcomeUnknown):
            self.service.publish(self.page_id, "brand")
        self.database.mark_unknown.assert_called_once()
        self.database.mark_failed.assert_not_called()
        alert = self.slack.notify_failure.call_args.kwargs
        self.assertFalse(alert["retry_safe"])
        self.assertEqual(alert["category"], "scheduling")

    def test_buffer_authentication_failure_is_definitive(self):
        self.buffer.schedule_text_post.side_effect = BufferAuthenticationError("HTTP 401")
        with self.assertRaises(PublishingError):
            self.service.publish(self.page_id, "brand")
        self.database.mark_failed.assert_called_once()
        self.database.mark_unknown.assert_not_called()
        alert = self.slack.notify_failure.call_args.kwargs
        self.assertEqual(alert["category"], "authentication")
        self.assertTrue(alert["retry_safe"])

    def test_notion_authentication_failure_is_alerted_before_buffer_write(self):
        self.notion.retrieve_page.side_effect = NotionAPIError(401, "unauthorized", "Denied")
        with self.assertRaises(Exception):
            self.service.publish(self.page_id, "brand")
        self.buffer.schedule_text_post.assert_not_called()
        alert = self.slack.notify_failure.call_args.kwargs
        self.assertEqual(alert["category"], "authentication")

    def test_notion_sync_failure_keeps_scheduled_job_and_allows_safe_replay(self):
        self.notion.update_page_properties.side_effect = RuntimeError("Notion unavailable")
        result = self.service.publish(self.page_id, "brand")
        self.assertFalse(result.notion_updated)
        self.database.mark_scheduled.assert_called_once()
        self.buffer.schedule_text_post.assert_called_once()
        alert = self.slack.notify_failure.call_args.kwargs
        self.assertEqual(alert["category"], "synchronization")
        self.assertTrue(alert["retry_safe"])

    def test_unknown_existing_job_blocks_another_buffer_mutation(self):
        self.database.reserve_job.return_value = (
            False,
            {"id": "job-old", "status": "unknown"},
        )
        with self.assertRaises(PublishingConflict):
            self.service.publish(self.page_id, "brand")
        self.buffer.schedule_text_post.assert_not_called()

    def test_changed_caption_after_reservation_prevents_mutation(self):
        changed_page = make_page(caption="Changed after approval")
        self.notion.retrieve_page.side_effect = [self.page, changed_page]
        with self.assertRaises(PublishingError):
            self.service.publish(self.page_id, "brand")
        self.buffer.schedule_text_post.assert_not_called()
        self.database.mark_failed.assert_called_once()


    def test_linkedin_uses_simulation_without_calling_buffer(self):
        page = make_page(platform="LinkedIn")
        self.notion.retrieve_page.return_value = page
        due = datetime.fromisoformat(page["properties"]["Scheduled Time"]["date"]["start"])
        service = PublishingService(
            notion_client=self.notion,
            buffer_client=self.buffer,
            database=self.database,
            slack=self.slack,
            channel_id="simulation:linkedin",
            platform="linkedin",
            authorized_approver_ids=(APPROVER,),
            authorized_founder_approver_ids=(),
            approval_snapshot_secret=SECRET,
        )

        result = service.publish(self.page_id, "brand")

        self.assertEqual(result.status, "scheduled")
        self.assertEqual(result.provider_post_id, "simulation-job-1")
        self.assertTrue(result.notion_updated)
        self.assertFalse(result.duplicate_request)
        self.buffer.schedule_text_post.assert_not_called()
        self.buffer.get_post.assert_not_called()
        reserve_kwargs = self.database.reserve_job.call_args.kwargs
        self.assertEqual(reserve_kwargs["provider"], "Simulation")
        self.assertEqual(reserve_kwargs["platform"], "linkedin")
        self.assertEqual(reserve_kwargs["channel_id"], "simulation:linkedin")
        self.assertEqual(self.database.start_execution.call_args.kwargs["provider"], "Simulation")
        self.database.mark_scheduled.assert_called_once()
        update = self.notion.update_page_properties.call_args.args[1]
        self.assertEqual(update["Provider"], {"select": {"name": "Simulation"}})


    def test_x_schedules_to_x_channel_and_replay_reuses_existing_post(self):
        page = make_page(platform="X")
        self.notion.retrieve_page.return_value = page
        due = datetime.fromisoformat(page["properties"]["Scheduled Time"]["date"]["start"])
        snapshot = page["properties"]["Approval Snapshot Hash"]["rich_text"][0]["plain_text"]
        self.buffer.schedule_text_post.return_value = {
            "id": "buffer-x-post-1",
            "status": "scheduled",
            "dueAt": due.isoformat(),
        }
        x_service = PublishingService(
            notion_client=self.notion,
            buffer_client=self.buffer,
            database=self.database,
            slack=self.slack,
            channel_id="x-buffer-channel",
            platform="x",
            authorized_approver_ids=(APPROVER,),
            authorized_founder_approver_ids=(),
            approval_snapshot_secret=SECRET,
        )

        first = x_service.publish(self.page_id, "brand")

        self.assertFalse(first.duplicate_request)
        self.assertEqual(first.provider_post_id, "buffer-x-post-1")
        self.buffer.schedule_text_post.assert_called_once_with(
            "x-buffer-channel", "Approved launch caption", due.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        reserve = self.database.reserve_job.call_args.kwargs
        self.assertEqual(reserve["platform"], "x")
        self.assertEqual(reserve["channel_id"], "x-buffer-channel")
        self.assertEqual(reserve["provider"], "Buffer")
        self.assertEqual(self.database.start_execution.call_args.kwargs["provider"], "Buffer")

        # Replay the exact same approved item and job: read existing Buffer post,
        # repair Notion, and never submit a second scheduling mutation.
        self.buffer.reset_mock()
        self.database.reset_mock()
        self.database.start_execution.return_value = "run-replay"
        self.database.reserve_job.return_value = (
            False,
            {
                "id": "job-x-1",
                "status": "scheduled",
                "provider": "Buffer",
                "provider_post_id": "buffer-x-post-1",
                "provider_status": "scheduled",
                "approval_snapshot_hash": snapshot,
                "scheduled_at": due,
            },
        )
        self.buffer.get_post.return_value = {
            "id": "buffer-x-post-1",
            "status": "scheduled",
            "dueAt": due.isoformat(),
        }

        replay = x_service.publish(self.page_id, "brand")

        self.assertTrue(replay.duplicate_request)
        self.assertEqual(replay.provider_post_id, "buffer-x-post-1")
        self.buffer.schedule_text_post.assert_not_called()
        self.buffer.get_post.assert_called_once_with("buffer-x-post-1")
        update = self.notion.update_page_properties.call_args.args[1]
        self.assertEqual(update["Provider"], {"select": {"name": "Buffer"}})
        self.assertEqual(update["Publishing Status"], {"select": {"name": "Scheduled"}})

    def test_linkedin_simulation_replay_does_not_query_buffer(self):
        page = make_page(platform="LinkedIn")
        self.notion.retrieve_page.return_value = page
        due = datetime.fromisoformat(page["properties"]["Scheduled Time"]["date"]["start"])
        snapshot = page["properties"]["Approval Snapshot Hash"]["rich_text"][0]["plain_text"]
        self.database.reserve_job.return_value = (
            False,
            {
                "id": "job-1",
                "status": "scheduled",
                "provider": "Simulation",
                "provider_post_id": "simulation-job-1",
                "provider_status": "scheduled",
                "approval_snapshot_hash": snapshot,
                "scheduled_at": due,
            },
        )
        service = PublishingService(
            notion_client=self.notion,
            buffer_client=self.buffer,
            database=self.database,
            slack=self.slack,
            channel_id="simulation:linkedin",
            platform="linkedin",
            authorized_approver_ids=(APPROVER,),
            authorized_founder_approver_ids=(),
            approval_snapshot_secret=SECRET,
        )

        result = service.publish(self.page_id, "brand")

        self.assertTrue(result.duplicate_request)
        self.assertEqual(result.provider_post_id, "simulation-job-1")
        self.buffer.schedule_text_post.assert_not_called()
        self.buffer.get_post.assert_not_called()
        self.database.mark_scheduled.assert_not_called()
        update = self.notion.update_page_properties.call_args.args[1]
        self.assertEqual(update["Provider"], {"select": {"name": "Simulation"}})
        self.assertEqual(update["Publishing Status"], {"select": {"name": "Scheduled"}})


if __name__ == "__main__":
    unittest.main()
