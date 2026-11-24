"""Optional Slack webhook alerts with plain-language recovery guidance."""
from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

_REDACTIONS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b(\s*[:=]\s*)[^\s,;]+"), r"\1\2[REDACTED]"),
    (re.compile(r"https?://hooks\.slack\.com/services/[^\s,;]+", re.I), "[REDACTED_SLACK_WEBHOOK]"),
)
LABELS = {
    "authentication": "Authentication",
    "scheduling": "Scheduling",
    "synchronization": "Notion synchronization",
    "persistence": "Database persistence",
    "workflow": "Publishing workflow",
}


def _safe(value: object, limit: int) -> str:
    text = str(value or "")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:limit]


class SlackNotifier:
    def __init__(self, webhook_url: str | None, timeout: int = 8):
        self.webhook_url = webhook_url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify_failure(
        self,
        *,
        job_id: str,
        page_id: str,
        reason: str,
        category: str = "workflow",
        recovery: str = "Check the publishing run and Notion record before taking action.",
        retry_safe: bool = False,
    ) -> bool:
        """Send one alert; a Slack outage never changes the publishing result."""
        if not self.webhook_url:
            return False
        retry_text = (
            "After fixing the cause, retry the same request."
            if retry_safe
            else "Do not retry automatically. Reconcile Buffer and the stored job first."
        )
        payload = {
            "text": (
                f":warning: *{LABELS.get(category, 'Publishing workflow')} failure*\n"
                f"Job: `{_safe(job_id, 100)}`\n"
                f"Notion page: `{_safe(page_id, 100)}`\n"
                f"What happened: {_safe(reason, 600)}\n"
                f"How to recover: {_safe(recovery, 500)}\n"
                f"Retry: {retry_text}"
            )
        }
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except Exception:
            return False
