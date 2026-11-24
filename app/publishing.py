"""One small, approval-gated Notion -> Buffer -> Notion publishing workflow."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.content import ContentValidationError, NotionContentReader
from app.buffer_client import (
    BufferAuthenticationError,
    BufferClient,
    BufferMutationRejected,
    BufferOutcomeUnknown,
)
from app.database import Database
from app.notion_client import NotionAPIError
from app.slack import SlackNotifier


class PublishingError(RuntimeError):
    pass


class PublishingConflict(PublishingError):
    pass


class PublishingOutcomeUnknown(PublishingError):
    pass


@dataclass(frozen=True)
class PublishOutcome:
    job_id: str
    status: str
    provider_post_id: str
    scheduled_at: datetime
    notion_updated: bool
    duplicate_request: bool


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_stored_time(value: datetime | str) -> datetime:
    """Normalize a PostgreSQL timestamp or ISO-8601 value to UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored scheduled_at must include a timezone.")
    return parsed.astimezone(timezone.utc)


class PublishingService:
    def __init__(
        self,
        *,
        notion_client,
        buffer_client: BufferClient,
        database: Database,
        slack: SlackNotifier,
        channel_id: str,
        platform: str,
        authorized_approver_ids: tuple[str, ...],
        authorized_founder_approver_ids: tuple[str, ...],
        approval_snapshot_secret: str,
    ):
        self.notion = notion_client
        self.buffer = buffer_client
        self.database = database
        self.slack = slack
        self.channel_id = channel_id
        self.platform = platform.lower()
        self.provider = "Simulation" if self.platform == "linkedin" else "Buffer"
        self.approver_ids = authorized_approver_ids
        self.founder_approver_ids = authorized_founder_approver_ids
        self.content_reader = NotionContentReader(
            notion_client,
            platform=platform,
            authorized_approver_ids=authorized_approver_ids,
            authorized_founder_approver_ids=authorized_founder_approver_ids,
            approval_snapshot_secret=approval_snapshot_secret,
        )
        self.active_job_id: str | None = None
        self.execution_context: dict[str, Any] = {}
        self.alert_sent = False

    @staticmethod
    def _authentication_provider(exc: BaseException | None) -> str | None:
        current = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, BufferAuthenticationError):
                return "Buffer"
            if isinstance(current, NotionAPIError) and current.status in {401, 403}:
                return "Notion"
            current = current.__cause__ or current.__context__
        return None

    def _notify_failure(
        self,
        *,
        page_id: str,
        reason: str,
        category: str,
        recovery: str,
        retry_safe: bool,
        exc: BaseException | None = None,
    ) -> None:
        """All operational alerts go through this best-effort path."""
        provider = self._authentication_provider(exc)
        if provider:
            category = "authentication"
            if provider == "Buffer":
                recovery = (
                    "Check BUFFER_API_KEY and the selected Buffer channel permissions. "
                    "A confirmed 401/403 did not create the requested post. Correct "
                    "access, then repeat the same request."
                )
            else:
                recovery = (
                    "Check NOTION_API_KEY and integration access to this page. Restore "
                    "access, then repeat the same request. If Buffer already has a post, "
                    "the saved provider ID will be reused."
                )
            retry_safe = True
        self.alert_sent = True
        try:
            self.slack.notify_failure(
                job_id=self.active_job_id or "not-reserved",
                page_id=page_id,
                reason=reason,
                category=category,
                recovery=recovery,
                retry_safe=retry_safe,
            )
        except Exception:
            # Slack is never allowed to mask the actual publishing result.
            pass

    def _read_content(self, page_id: str, content_type: str):
        try:
            return self.content_reader.read(page_id, content_type)
        except ContentValidationError as exc:
            raise PublishingError(str(exc)) from exc

    def _sync_notion(self, page_id: str, post: dict[str, Any], fallback_time: datetime) -> tuple[str, datetime]:
        post_id = str(post.get("id") or "")
        provider_status = str(post.get("status") or "scheduled").lower()
        if not post_id:
            raise PublishingError("Buffer post ID is missing; status cannot be synchronized.")
        if provider_status == "sent":
            notion_status = "Published"
        elif provider_status in {"scheduled", "sending"}:
            notion_status = "Scheduled"
        else:
            raise PublishingError(
                f"Buffer returned unsupported post status {provider_status!r}; Notion was not changed."
            )

        scheduled_at = fallback_time
        provider_due = post.get("dueAt")
        if provider_due:
            try:
                parsed = datetime.fromisoformat(str(provider_due).replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    scheduled_at = parsed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass

        self.notion.update_page_properties(
            page_id,
            {
                "Provider": {"select": {"name": self.provider}},
                "Provider Post ID": {"rich_text": [{"type": "text", "text": {"content": post_id}}]},
                "Publishing Status": {"select": {"name": notion_status}},
                "Scheduled Time": {"date": {"start": _iso(scheduled_at)}},
                "Published URL": {"url": post.get("externalLink") if notion_status == "Published" else None},
                "Failure Reason": {"rich_text": []},
            },
        )
        return notion_status.lower(), scheduled_at

    def _mark_failed_in_notion(self, page_id: str, reason: str) -> str | None:
        try:
            self.notion.update_page_properties(
                page_id,
                {
                    "Provider": {"select": {"name": self.provider}},
                    "Publishing Status": {"select": {"name": "Failed"}},
                    "Failure Reason": {
                        "rich_text": [{"type": "text", "text": {"content": reason[:1900]}}]
                    },
                },
            )
            return None
        except Exception as exc:
            return str(exc)

    def _mark_unknown_in_notion(
        self,
        page_id: str,
        reason: str,
        provider_post_id: str | None = None,
    ) -> str | None:
        """Make an ambiguous provider result visible without claiming it failed."""
        properties: dict[str, Any] = {
            "Provider": {"select": {"name": self.provider}},
            "Publishing Status": {"select": {"name": "Needs Reconciliation"}},
            "Failure Reason": {
                "rich_text": [{"type": "text", "text": {"content": reason[:1900]}}]
            },
        }
        if provider_post_id:
            properties["Provider Post ID"] = {
                "rich_text": [{"type": "text", "text": {"content": provider_post_id}}]
            }
        try:
            self.notion.update_page_properties(page_id, properties)
            return None
        except Exception as exc:
            return str(exc)

    def publish(self, page_id: str, content_type: str) -> PublishOutcome:
        return self._execute_with_audit(page_id, content_type)

    def _execute_with_audit(self, page_id: str, content_type: str) -> PublishOutcome:
        self.active_job_id = None
        self.execution_context = {}
        self.alert_sent = False
        run_id = self.database.start_execution(
            content_id=page_id,
            source="publishing_api",
            inputs={"notion_page_id": page_id, "content_type": content_type},
            provider=self.provider,
        )
        try:
            outcome = self._publish(page_id, content_type)
            context = self.execution_context
            self.database.finish_execution(
                run_id,
                status="needs_attention" if not outcome.notion_updated else "succeeded",
                job_id=outcome.job_id,
                content_id=context.get("content_id"),
                inputs=context.get("inputs"),
                outputs={
                    "job_id": outcome.job_id,
                    "provider_post_id": outcome.provider_post_id,
                    "status": outcome.status,
                    "scheduled_at": _iso(outcome.scheduled_at),
                    "notion_updated": outcome.notion_updated,
                    "duplicate_request": outcome.duplicate_request,
                },
                approval_details=context.get("approval_details"),
                error=None if outcome.notion_updated else "Buffer succeeded; Notion synchronization needs repair.",
            )
            return outcome
        except Exception as exc:
            status = "unknown" if isinstance(exc, PublishingOutcomeUnknown) else "failed"
            context = self.execution_context
            self.database.finish_execution(
                run_id,
                status=status,
                job_id=self.active_job_id,
                content_id=context.get("content_id"),
                inputs=context.get("inputs"),
                outputs={"operation": "schedule"},
                approval_details=context.get("approval_details"),
                error=f"{type(exc).__name__}: {exc}",
            )
            if not self.alert_sent and not isinstance(exc, PublishingConflict):
                provider = self._authentication_provider(exc)
                if provider:
                    category = "authentication"
                    recovery = "Correct the integration credentials/permissions, then replay the same request."
                    retry_safe = True
                elif isinstance(exc, PublishingOutcomeUnknown):
                    category = "scheduling"
                    recovery = (
                        "Buffer may have accepted the request. Do not submit again. Search the selected "
                        "Buffer channel for this post and reconcile its ID against the job first."
                    )
                    retry_safe = False
                else:
                    category = "workflow"
                    recovery = (
                        "Correct the Notion approval, content or schedule issue. Re-check the page before "
                        "submitting again."
                    )
                    retry_safe = True
                self._notify_failure(
                    page_id=page_id,
                    reason=str(exc),
                    category=category,
                    recovery=recovery,
                    retry_safe=retry_safe,
                    exc=exc,
                )
            raise

    def _publish(self, page_id: str, content_type: str) -> PublishOutcome:
        content = self._read_content(page_id, content_type)
        idempotency_key = hashlib.sha256(
            f"{page_id}|{self.channel_id}|{self.platform}|{content.snapshot_hash}|{_iso(content.scheduled_at)}".encode("utf-8")
        ).hexdigest()

        created, job = self.database.reserve_job(
            idempotency_key=idempotency_key,
            notion_page_id=page_id,
            content_type=content_type,
            platform=self.platform,
            channel_id=self.channel_id,
            approval_snapshot_hash=content.snapshot_hash,
            provider=self.provider,
            scheduled_at=_iso(content.scheduled_at),
        )
        self.active_job_id = str(job["id"])

        if not created:
            existing_status = job["status"]
            if existing_status == "scheduled" and job.get("provider_post_id"):
                if (
                    job.get("approval_snapshot_hash") != content.snapshot_hash
                    or _parse_stored_time(job["scheduled_at"]) != content.scheduled_at
                ):
                    raise PublishingConflict(
                        "An existing Buffer post belongs to a different approval snapshot or schedule."
                    )
                try:
                    if self.provider == "Simulation":
                        post = {
                            "id": str(job["provider_post_id"]),
                            "status": str(job.get("provider_status") or "scheduled"),
                            "dueAt": _iso(content.scheduled_at),
                            "externalLink": None,
                        }
                    else:
                        post = self.buffer.get_post(str(job["provider_post_id"]))
                    if str(post.get("id")) != str(job["provider_post_id"]):
                        raise PublishingError("Buffer returned a different post ID during reconciliation.")
                    status, scheduled_at = self._sync_notion(page_id, post, content.scheduled_at)
                    return PublishOutcome(
                        job_id=str(job["id"]),
                        status=status,
                        provider_post_id=str(job["provider_post_id"]),
                        scheduled_at=scheduled_at,
                        notion_updated=True,
                        duplicate_request=True,
                    )
                except Exception as exc:
                    self._notify_failure(
                        page_id=page_id,
                        reason=f"Existing Buffer post could not be reconciled or synced to Notion: {exc}",
                        category="synchronization",
                        recovery=(
                            "Do not create another post. Restore Buffer/Notion access and replay this same "
                            "request; the stored provider post ID will be reused."
                        ),
                        retry_safe=True,
                        exc=exc,
                    )
                    return PublishOutcome(
                        job_id=str(job["id"]),
                        status="scheduled",
                        provider_post_id=str(job["provider_post_id"]),
                        scheduled_at=content.scheduled_at,
                        notion_updated=False,
                        duplicate_request=True,
                    )

            if existing_status == "failed" and content.notion_status == "Ready":
                if self.database.reopen_failed_job(job["id"]):
                    created = True
                else:
                    raise PublishingConflict("This failed job is already being retried.")
            else:
                raise PublishingConflict(
                    f"An existing job is {existing_status!r}. No second Buffer request was sent. "
                    "Reconcile that job before proceeding."
                )

        if content.notion_status != "Ready":
            reason = f"Publishing Status is {content.notion_status!r}; a new post requires Ready."
            self.database.mark_failed(job["id"], reason)
            raise PublishingError(reason)

        # Re-fetch, re-validate and compare the approval signature immediately before
        # the external mutation. This prevents publishing content changed after reservation.
        fresh = self._read_content(page_id, content_type)
        if (
            fresh.notion_status != "Ready"
            or fresh.snapshot_hash != content.snapshot_hash
            or fresh.caption != content.caption
            or fresh.scheduled_at != content.scheduled_at
        ):
            reason = "Notion content, approval or schedule changed after reservation; Buffer was not called."
            self.database.mark_failed(job["id"], reason)
            raise PublishingError(reason)

        try:
            if self.provider == "Simulation":
                # No external post is created for LinkedIn; the stable ID is derived
                # from the already-reserved PostgreSQL job so replay is idempotent.
                post = {
                    "id": f"simulation-{job['id']}",
                    "status": "scheduled",
                    "dueAt": _iso(fresh.scheduled_at),
                    "externalLink": None,
                }
            else:
                post = self.buffer.schedule_text_post(
                    self.channel_id,
                    fresh.caption,
                    _iso(fresh.scheduled_at),
                )
        except (BufferAuthenticationError, BufferMutationRejected) as exc:
            self.database.mark_failed(job["id"], str(exc))
            notion_error = self._mark_failed_in_notion(page_id, str(exc))
            message = f"Buffer definitively rejected scheduling: {exc}"
            if notion_error:
                message += f" Notion could not be marked Failed: {notion_error}"
            raise PublishingError(message) from exc
        except BufferOutcomeUnknown as exc:
            self.database.mark_unknown(job["id"], str(exc))
            notion_error = self._mark_unknown_in_notion(page_id, str(exc))
            detail = "Buffer scheduling outcome is unknown. Do not retry until the provider is reconciled."
            if notion_error:
                detail += f" Notion could not be updated: {notion_error}"
            raise PublishingOutcomeUnknown(detail) from exc
        except Exception as exc:
            reason = f"Unexpected error after Buffer submission began: {type(exc).__name__}: {exc}"
            self.database.mark_unknown(job["id"], reason)
            notion_error = self._mark_unknown_in_notion(page_id, reason)
            detail = "Unexpected Buffer error; the job is quarantined to prevent a duplicate post."
            if notion_error:
                detail += f" Notion could not be updated: {notion_error}"
            raise PublishingOutcomeUnknown(detail) from exc

        provider_post_id = str(post["id"])
        provider_status = str(post.get("status") or "scheduled").lower()
        try:
            self.database.mark_scheduled(
                job["id"],
                provider_post_id,
                provider_status,
                _iso(fresh.scheduled_at),
            )
        except Exception as exc:
            # The provider accepted the mutation but local persistence failed.
            reason = f"Buffer created post {provider_post_id}, but local persistence failed: {exc}"
            try:
                self.database.mark_unknown(job["id"], reason)
            except Exception:
                pass
            notion_error = self._mark_unknown_in_notion(page_id, reason, provider_post_id)
            detail = (
                f"Buffer created post {provider_post_id}, but its ID could not be persisted. Do not retry."
            )
            if notion_error:
                detail += f" Notion could not be updated: {notion_error}"
            raise PublishingOutcomeUnknown(detail) from exc

        try:
            status, scheduled_at = self._sync_notion(page_id, post, fresh.scheduled_at)
            return PublishOutcome(
                job_id=str(job["id"]),
                status=status,
                provider_post_id=provider_post_id,
                scheduled_at=scheduled_at,
                notion_updated=True,
                duplicate_request=False,
            )
        except Exception as exc:
            self._notify_failure(
                page_id=page_id,
                reason=f"Buffer post {provider_post_id} exists, but Notion status synchronization failed: {exc}",
                category="synchronization",
                recovery=(
                    "The Buffer post ID is already saved. Restore Notion access and replay the identical "
                    "request; the existing post will be reused and no duplicate will be created."
                ),
                retry_safe=True,
                exc=exc,
            )
            return PublishOutcome(
                job_id=str(job["id"]),
                status="scheduled",
                provider_post_id=provider_post_id,
                scheduled_at=fresh.scheduled_at,
                notion_updated=False,
                duplicate_request=False,
            )
