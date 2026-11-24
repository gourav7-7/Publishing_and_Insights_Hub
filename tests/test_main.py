from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.publishing import PublishOutcome, PublishingService


class _FakeDatabase:
    def __init__(self, _database_url: str):
        pass

    def initialize_schema(self) -> None:
        pass


class _FakeNotionClient:
    def __init__(self, *args):
        pass


class _FakeBufferClient:
    def __init__(self, *args):
        # Construction stores settings only; no request is made.
        pass


class _FakeSlackNotifier:
    def __init__(self, *args):
        pass

    def notify_failure(self, **kwargs):
        pass


class MultiPlatformRouteTests(unittest.TestCase):
    def test_api_routes_each_platform_to_its_configured_channel_and_provider(self):
        settings = SimpleNamespace(
            notion_api_key="test-notion-key",
            notion_version="2026-03-11",
            notion_timeout_seconds=20,
            database_url="postgresql://test-only",
            buffer_api_key="test-buffer-key",
            buffer_channel_id="threads-buffer-channel",
            buffer_channel_id_x="x-buffer-channel",
            buffer_timeout_seconds=20,
            publishing_platform="threads",
            publishing_api_token="test-publishing-token",
            enable_live_scheduling=True,
            authorized_approver_ids=("test-approver",),
            authorized_founder_approver_ids=(),
            approval_snapshot_secret="s" * 32,
            slack_webhook_url=None,
        )
        captured: list[tuple[str, str, str]] = []

        def fake_publish(service, page_id: str, content_type: str) -> PublishOutcome:
            captured.append((service.platform, service.channel_id, service.provider))
            return PublishOutcome(
                job_id="local-test-job",
                status="scheduled",
                provider_post_id=f"local-{service.platform}-post",
                scheduled_at=datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc),
                notion_updated=True,
                duplicate_request=False,
            )

        with (
            patch.object(main_module, "get_settings", return_value=settings),
            patch.object(main_module, "Database", _FakeDatabase),
            patch.object(main_module, "NotionClient", _FakeNotionClient),
            patch.object(main_module, "BufferClient", _FakeBufferClient),
            patch.object(main_module, "SlackNotifier", _FakeSlackNotifier),
            patch.object(PublishingService, "publish", fake_publish),
        ):
            with TestClient(main_module.create_app()) as client:
                for platform in ("threads", "x", "linkedin"):
                    response = client.post(
                        "/api/v1/publishing/jobs",
                        headers={"X-Publishing-Token": "test-publishing-token"},
                        json={
                            "page_id": "a" * 32,
                            "content_type": "brand",
                            "platform": platform,
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(
            captured,
            [
                ("threads", "threads-buffer-channel", "Buffer"),
                ("x", "x-buffer-channel", "Buffer"),
                ("linkedin", "simulation:linkedin", "Simulation"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
