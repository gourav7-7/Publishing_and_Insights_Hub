"""FastAPI entry point. Notion remains the source of truth; no custom UI is needed."""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from app.buffer_client import BufferClient
from app.config import get_settings
from app.database import Database
from app.models import HealthResponse, PublishRequest, PublishResponse
from app.notion_client import NotionClient
from app.publishing import (
    PublishingConflict,
    PublishingError,
    PublishingOutcomeUnknown,
    PublishingService,
)
from app.slack import SlackNotifier


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = get_settings()
        database = Database(settings.database_url)
        database.initialize_schema()
        application.state.settings = settings
        application.state.database = database
        yield

    application = FastAPI(
        title="Just Sortd Publishing Hub",
        version="1.0.0",
        description="Approval-gated Notion to social scheduler publishing bridge.",
        lifespan=lifespan,
    )

    @application.get("/health/live")
    def live():
        return {"status": "ok"}

    @application.get("/health/ready", response_model=HealthResponse)
    def ready(request: Request):
        database = getattr(request.app.state, "database", None)
        if database is None or not database.ping():
            return HealthResponse(status="degraded", storage="unavailable")
        return HealthResponse(status="ok", storage="connected")

    @application.post("/api/v1/publishing/jobs", response_model=PublishResponse)
    def publish(
        payload: PublishRequest,
        request: Request,
        x_publishing_token: str | None = Header(default=None, alias="X-Publishing-Token"),
    ):
        settings = request.app.state.settings
        if not x_publishing_token or not secrets.compare_digest(
            x_publishing_token, settings.publishing_api_token
        ):
            SlackNotifier(settings.slack_webhook_url).notify_failure(
                job_id="not-created",
                page_id=payload.page_id,
                reason="A publishing request supplied an invalid API token.",
                category="authentication",
                recovery="Check the calling automation's PUBLISHING_API_TOKEN. Do not share the token in Slack.",
                retry_safe=True,
            )
            raise HTTPException(status_code=401, detail="Invalid publishing API token.")

        if not settings.enable_live_scheduling:
            raise HTTPException(
                status_code=423,
                detail="Live scheduling is disabled. Enable it only for the verified test account.",
            )

        platform = (payload.platform or settings.publishing_platform).lower()
        if platform == "threads":
            channel_id = settings.buffer_channel_id
        elif platform == "x":
            channel_id = settings.buffer_channel_id_x
        elif platform == "linkedin":
            # Stable synthetic channel key keeps LinkedIn jobs isolated in PostgreSQL.
            channel_id = "simulation:linkedin"
        else:
            raise HTTPException(
                status_code=422,
                detail="This demo supports Threads, X and simulated LinkedIn only.",
            )

        service = PublishingService(
            notion_client=NotionClient(
                settings.notion_api_key,
                settings.notion_version,
                settings.notion_timeout_seconds,
            ),
            buffer_client=BufferClient(
                settings.buffer_api_key,
                settings.buffer_timeout_seconds,
            ),
            database=request.app.state.database,
            slack=SlackNotifier(settings.slack_webhook_url),
            channel_id=channel_id,
            platform=platform,
            authorized_approver_ids=settings.authorized_approver_ids,
            authorized_founder_approver_ids=settings.authorized_founder_approver_ids,
            approval_snapshot_secret=settings.approval_snapshot_secret,
        )

        try:
            outcome = service.publish(payload.page_id, payload.content_type)
        except PublishingOutcomeUnknown as exc:
            raise HTTPException(
                status_code=202,
                detail={"status": "unknown", "message": str(exc), "retry": False},
            ) from exc
        except PublishingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublishingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="Publishing failed. Check the workflow run and job state before retrying.",
            ) from exc

        provider_label = "LinkedIn simulation" if platform == "linkedin" else f"Buffer {platform.upper()} post"
        message = (
            f"{provider_label} scheduled and Notion updated."
            if outcome.notion_updated
            else f"{provider_label} exists; Notion synchronization needs attention. Replay the same request after fixing access."
        )
        if outcome.duplicate_request:
            message = (
                f"Existing {provider_label} reconciled; no duplicate was created."
                if outcome.notion_updated
                else f"Existing {provider_label} reused; Notion synchronization still needs attention."
            )

        return PublishResponse(
            job_id=outcome.job_id,
            status=outcome.status,
            provider_post_id=outcome.provider_post_id,
            scheduled_at=outcome.scheduled_at.isoformat(),
            notion_updated=outcome.notion_updated,
            duplicate_request=outcome.duplicate_request,
            message=message,
        )

    return application


app = create_app()
