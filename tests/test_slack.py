from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.slack import SlackNotifier


class SlackNotifierTests(unittest.TestCase):
    @patch("app.slack.urlopen")
    def test_sends_recovery_message_and_redacts_secrets(self, mocked_urlopen):
        mocked_urlopen.return_value.__enter__.return_value.status = 200
        notifier = SlackNotifier("https://hooks.slack.com/services/test/webhook")
        sent = notifier.notify_failure(
            job_id="job-1",
            page_id="page-1",
            reason="Bearer super-secret api_key=abc123",
            category="authentication",
            recovery="Fix the Buffer credentials, then retry the same request.",
            retry_safe=True,
        )
        self.assertTrue(sent)
        request = mocked_urlopen.call_args.args[0]
        message = json.loads(request.data)["text"]
        self.assertIn("Authentication failure", message)
        self.assertIn("Fix the Buffer credentials", message)
        self.assertIn("retry the same request", message)
        self.assertNotIn("super-secret", message)
        self.assertNotIn("abc123", message)

    def test_disabled_slack_returns_false(self):
        self.assertFalse(SlackNotifier(None).notify_failure(
            job_id="job-1", page_id="page-1", reason="test"
        ))

    @patch("app.slack.urlopen", side_effect=OSError("unavailable"))
    def test_slack_failure_does_not_raise(self, mocked_urlopen):
        result = SlackNotifier("https://hooks.slack.com/services/test/webhook").notify_failure(
            job_id="job-1", page_id="page-1", reason="test"
        )
        self.assertFalse(result)
        mocked_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
