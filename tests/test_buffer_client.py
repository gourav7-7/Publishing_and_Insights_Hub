from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from app.buffer_client import (
    BufferAuthenticationError,
    BufferClient,
    BufferMutationRejected,
    BufferOutcomeUnknown,
)


class FakeResponse:
    status = 200

    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class BufferClientTests(unittest.TestCase):
    @patch("app.buffer_client.urlopen")
    def test_schedule_uses_exact_custom_utc_time(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-1", "status": "scheduled"},
        }}})
        post = BufferClient("token").schedule_text_post(
            "channel-1", "approved caption", "2026-12-01T10:00:00Z"
        )
        self.assertEqual(post["id"], "post-1")
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        data = payload["variables"]["input"]
        self.assertEqual(data["mode"], "customScheduled")
        self.assertEqual(data["dueAt"], "2026-12-01T10:00:00Z")
        self.assertNotIn("saveToDraft", data)

    @patch("app.buffer_client.urlopen")
    def test_explicit_mutation_error_is_definitive(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse({"data": {"createPost": {
            "__typename": "MutationError",
            "message": "Channel is not permitted",
        }}})
        with self.assertRaises(BufferMutationRejected):
            BufferClient("token").schedule_text_post(
                "channel-1", "caption", "2026-12-01T10:00:00Z"
            )
        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch("app.buffer_client.urlopen")
    def test_mutation_authentication_error_is_definitive(self, mocked_urlopen):
        mocked_urlopen.side_effect = HTTPError(
            "https://api.buffer.com", 401, "Unauthorized", {}, None
        )
        with self.assertRaises(BufferAuthenticationError):
            BufferClient("token").schedule_text_post(
                "channel-1", "caption", "2026-12-01T10:00:00Z"
            )
        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch("app.buffer_client.urlopen")
    def test_non_auth_graphql_error_is_unknown_not_retried(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse({
            "errors": [{"message": "Internal error"}]
        })
        with self.assertRaises(BufferOutcomeUnknown):
            BufferClient("token").schedule_text_post(
                "channel-1", "caption", "2026-12-01T10:00:00Z"
            )
        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch("app.buffer_client.urlopen")
    def test_transport_failure_after_submit_is_unknown_not_retried(self, mocked_urlopen):
        mocked_urlopen.side_effect = URLError("connection lost")
        with self.assertRaises(BufferOutcomeUnknown):
            BufferClient("token").schedule_text_post(
                "channel-1", "caption", "2026-12-01T10:00:00Z"
            )
        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch("app.buffer_client.urlopen")
    def test_get_post_reads_provider_lifecycle_fields(self, mocked_urlopen):
        expected = {
            "id": "post-2",
            "status": "sent",
            "channelId": "channel-1",
            "externalLink": "https://example.test/post/2",
        }
        mocked_urlopen.return_value = FakeResponse({"data": {"post": expected}})
        result = BufferClient("token").get_post("post-2")
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
