from __future__ import annotations

import unittest
from unittest.mock import patch

from app.database import Database


class _RecordingCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return self.row


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor


class DatabaseUnitTests(unittest.TestCase):
    def test_requires_database_url(self):
        with self.assertRaises(ValueError):
            Database(" ")

    def test_redacts_credentials_from_errors(self):
        safe = Database.redact(
            "Request failed with Bearer token123 api_key=secret-value "
            "https://hooks.slack.com/services/T/B/secret"
        )
        self.assertNotIn("token123", safe)
        self.assertNotIn("secret-value", safe)
        self.assertNotIn("T/B/secret", safe)
        self.assertIn("[REDACTED]", safe)

    def test_reservation_binds_platform_channel_and_provider_to_job_insert(self):
        cursor = _RecordingCursor({"id": "job-1", "provider": "Simulation"})
        database = Database("postgresql://test-only")
        with patch.object(database, "_connect", return_value=_RecordingConnection(cursor)):
            created, job = database.reserve_job(
                idempotency_key="a" * 64,
                notion_page_id="notion-page-1",
                content_type="brand",
                platform="linkedin",
                channel_id="simulation:linkedin",
                approval_snapshot_hash="snapshot-hash",
                scheduled_at="2026-12-01T12:00:00Z",
                provider="Simulation",
            )

        self.assertTrue(created)
        self.assertEqual(job["provider"], "Simulation")
        query, params = cursor.calls[0]
        self.assertIn("platform,", query)
        self.assertIn("channel_id, provider", query)
        self.assertEqual(params[3:6], ("linkedin", "simulation:linkedin", "Simulation"))

    def test_workflow_run_insert_binds_simulation_provider(self):
        cursor = _RecordingCursor({"run_id": "run-1"})
        database = Database("postgresql://test-only")
        with patch.object(database, "_connect", return_value=_RecordingConnection(cursor)):
            run_id = database.start_execution(
                content_id="notion-page-1",
                source="publishing_api",
                inputs={"notion_page_id": "notion-page-1"},
                provider="Simulation",
            )

        self.assertEqual(run_id, "run-1")
        query, params = cursor.calls[0]
        self.assertIn("workflow_runs(content_id,source,inputs,provider,status)", query)
        self.assertEqual(params[3], "Simulation")

    def test_safely_redacts_nested_run_data(self):
        value = Database._safe_json({
            "details": ["Bearer my-token", {"api_key": "another-secret"}],
            "safe": "no secret here",
        })
        self.assertNotIn("my-token", str(value))
        self.assertNotIn("another-secret", str(value))
        self.assertEqual(value["safe"], "no secret here")


if __name__ == "__main__":
    unittest.main()
