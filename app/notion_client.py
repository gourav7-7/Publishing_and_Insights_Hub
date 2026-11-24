"""Minimal Notion API client for reading pages and updating page properties."""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class NotionAPIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"Notion API error {status} ({code}): {message}")


class NotionClient:
    BASE_URL = "https://api.notion.com/v1"
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 529}

    def __init__(self, api_key: str, version: str = "2026-03-11", timeout: int = 20):
        if not api_key.strip():
            raise ValueError("Notion API key is required.")
        if not version.strip():
            raise ValueError("Notion API version is required.")
        if not 1 <= timeout <= 120:
            raise ValueError("Notion timeout must be between 1 and 120 seconds.")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Notion-Version": version.strip(),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(4):
            request = Request(
                f"{self.BASE_URL}{endpoint}",
                data=body,
                headers=self.headers,
                method=method.upper(),
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    error_data = json.loads(raw)
                except json.JSONDecodeError:
                    error_data = {}
                if exc.code in self.RETRYABLE_STATUSES and attempt < 3:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 2 ** attempt
                    except (TypeError, ValueError):
                        delay = 2 ** attempt
                    time.sleep(min(max(delay, 1.0), 8.0))
                    continue
                raise NotionAPIError(
                    exc.code,
                    str(error_data.get("code", "unknown_error")),
                    str(error_data.get("message", raw[:500])),
                ) from None
            except URLError as exc:
                raise RuntimeError(f"Could not reach Notion API: {exc.reason}") from None
            except TimeoutError:
                raise RuntimeError("Notion API request timed out.") from None
            except json.JSONDecodeError:
                raise RuntimeError("Notion API returned invalid JSON.") from None
        raise RuntimeError("Notion API request failed after retries.")

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def update_page_properties(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/pages/{page_id}",
            payload={"properties": properties},
        )
