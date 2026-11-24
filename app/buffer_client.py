"""Small Buffer GraphQL adapter. Mutations are never retried automatically."""
from __future__ import annotations

import json
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BUFFER_ENDPOINT = "https://api.buffer.com"
RETRYABLE_READ_STATUSES = {429, 500, 502, 503, 504, 529}
POST_STATUSES = ["error", "scheduled", "sending", "sent"]

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status dueAt externalLink sentAt } }
    ... on MutationError { message }
  }
}
"""

GET_POST = """
query GetPost($input: PostInput!) {
  post(input: $input) {
    id status channelId text dueAt sentAt externalLink createdAt
  }
}
"""

LIST_POSTS = """
query ListRecentPosts($input: PostsInput!, $first: Int!, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    edges { node { id status channelId text dueAt sentAt externalLink createdAt } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class BufferError(RuntimeError):
    """Base class for Buffer errors."""


class BufferAuthenticationError(BufferError):
    """Buffer definitively rejected credentials or permissions."""


class BufferQueryError(BufferError):
    """A read-only Buffer query failed."""


class BufferMutationRejected(BufferError):
    """Buffer definitively rejected a mutation."""


class BufferOutcomeUnknown(BufferError):
    """A mutation may have reached Buffer; reconcile before retrying."""


class BufferClient:
    def __init__(self, api_key: str, timeout: int = 20, endpoint: str = BUFFER_ENDPOINT):
        if not api_key.strip():
            raise ValueError("Buffer API key is required.")
        if not 1 <= timeout <= 120:
            raise ValueError("Buffer timeout must be between 1 and 120 seconds.")
        self._api_key = api_key.strip()
        self.timeout = timeout
        self.endpoint = endpoint

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _delay(attempt: int, retry_after: str | None = None) -> float:
        try:
            if retry_after:
                return min(max(float(retry_after), 0.25), 8.0)
        except (TypeError, ValueError):
            pass
        return min(0.5 * (2 ** attempt), 4.0)

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        lowered = message.lower()
        return any(term in lowered for term in (
            "unauthorized", "forbidden", "invalid api key", "authentication"
        ))

    def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Read-only queries use a small retry/backoff budget."""
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        for attempt in range(3):
            request = Request(
                self.endpoint,
                data=payload,
                headers=self._headers(),
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise BufferAuthenticationError(
                        f"Buffer authentication/authorization failed (HTTP {exc.code})."
                    ) from None
                if exc.code in RETRYABLE_READ_STATUSES and attempt < 2:
                    time.sleep(self._delay(attempt, exc.headers.get("Retry-After")))
                    continue
                raise BufferQueryError(
                    f"Buffer read failed with HTTP {exc.code}; no provider write was attempted."
                ) from None
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt < 2:
                    time.sleep(self._delay(attempt))
                    continue
                raise BufferQueryError(
                    f"Buffer read could not complete after retries ({type(exc).__name__})."
                ) from None

            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                raise BufferQueryError("Buffer read returned invalid JSON.") from None
            if not isinstance(body, dict):
                raise BufferQueryError("Buffer read returned an unexpected response shape.")
            errors = body.get("errors") or []
            if errors:
                message = "; ".join(
                    str(item.get("message", "GraphQL error"))
                    for item in errors if isinstance(item, dict)
                )[:300]
                if self._is_auth_error(message):
                    raise BufferAuthenticationError(
                        "Buffer rejected the API credentials or query permissions."
                    )
                raise BufferQueryError(
                    "Buffer rejected a read query; check API permissions and query compatibility."
                )
            data = body.get("data")
            if not isinstance(data, dict):
                raise BufferQueryError("Buffer read did not return a data object.")
            return data
        raise BufferQueryError("Buffer read query exhausted its retry limit.")

    def _create_post(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Submit one mutation. Do not retry after the request may have been sent."""
        payload = json.dumps({
            "query": CREATE_POST,
            "variables": {"input": input_data},
        }).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise BufferAuthenticationError(
                    f"Buffer authentication/authorization failed (HTTP {exc.code})."
                ) from None
            raise BufferOutcomeUnknown(
                f"Buffer returned HTTP {exc.code}; provider outcome requires reconciliation."
            ) from None
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise BufferOutcomeUnknown(
                f"Buffer transport failed after submission began ({type(exc).__name__}); outcome is unknown."
            ) from None

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise BufferOutcomeUnknown(
                "Buffer returned a non-JSON response; outcome is unknown."
            ) from None
        if not isinstance(body, dict):
            raise BufferOutcomeUnknown(
                "Buffer returned an unexpected response shape; outcome is unknown."
            )

        if body.get("errors"):
            message = "; ".join(
                str(item.get("message", "GraphQL error"))
                for item in body["errors"] if isinstance(item, dict)
            )[:300]
            if self._is_auth_error(message):
                raise BufferAuthenticationError(
                    "Buffer rejected the API credentials or mutation permissions."
                )
            raise BufferOutcomeUnknown(
                "Buffer GraphQL response contained errors: " + message
            )

        result = (body.get("data") or {}).get("createPost") or {}
        if result.get("__typename") == "MutationError":
            message = str(result.get("message") or "Buffer rejected the post.")
            if self._is_auth_error(message):
                raise BufferAuthenticationError(
                    "Buffer rejected the API credentials or scheduling permission."
                )
            raise BufferMutationRejected(message[:500])

        post = result.get("post")
        if (
            result.get("__typename") != "PostActionSuccess"
            or not isinstance(post, dict)
            or not post.get("id")
        ):
            raise BufferOutcomeUnknown(
                "Buffer response did not contain a confirmed success and post ID."
            )
        return post

    def schedule_text_post(self, channel_id: str, text: str, due_at_utc: str) -> dict[str, Any]:
        if not channel_id.strip() or not text.strip() or not due_at_utc.strip():
            raise ValueError("channel_id, text and due_at_utc are required.")
        return self._create_post({
            "channelId": channel_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": due_at_utc,
        })

    def get_post(self, post_id: str) -> dict[str, Any]:
        if not post_id.strip():
            raise ValueError("post_id is required.")
        post = self._query(GET_POST, {"input": {"id": post_id.strip()}}).get("post")
        if not isinstance(post, dict) or not post.get("id"):
            raise BufferQueryError("Buffer did not return the requested post.")
        return post

    def list_recent_posts(
        self,
        *,
        organization_id: str,
        channel_id: str,
        created_after: str,
        page_size: int = 50,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        """List posts for manual reconciliation after an ambiguous mutation."""
        if not organization_id.strip() or not channel_id.strip() or not created_after.strip():
            raise ValueError("organization_id, channel_id and created_after are required.")
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 20:
            raise ValueError("page_size must be 1-100 and max_pages must be 1-20.")
        query_input = {
            "organizationId": organization_id.strip(),
            "filter": {
                "channelIds": [channel_id.strip()],
                "status": POST_STATUSES,
                "createdAt": {"start": created_after},
            },
        }
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = self._query(
                LIST_POSTS,
                {"input": query_input, "first": page_size, "after": cursor},
            )
            connection = data.get("posts")
            if not isinstance(connection, dict):
                raise BufferQueryError("Buffer returned an invalid posts connection.")
            for edge in connection.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    results.append(node)
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return results
            cursor = page_info.get("endCursor")
            if not cursor:
                raise BufferQueryError(
                    "Buffer indicated more results but did not return a pagination cursor."
                )
        raise BufferQueryError(
            f"Reconciliation exceeded the {max_pages}-page safety limit."
        )
