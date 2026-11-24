"""Request and response models for the publishing API."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class PublishRequest(BaseModel):
    page_id: str = Field(min_length=32, max_length=64)
    content_type: Literal["brand", "founder"] = "brand"
    # Omit to use the existing PUBLISHING_PLATFORM default.
    platform: Literal["threads", "x", "linkedin"] | None = None


class PublishResponse(BaseModel):
    job_id: str
    status: Literal["scheduled", "published"]
    provider_post_id: str
    scheduled_at: str
    notion_updated: bool
    duplicate_request: bool
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    storage: Literal["connected", "unavailable"]
