"""Application settings loaded from environment variables and a local .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUPPORTED_PLATFORMS = {
    "threads",
    "x",
    "linkedin",
}


def load_env_file(path: Path | None = None) -> None:
    """Load .env values without overriding variables already in the environment."""
    env_path = path or PROJECT_ROOT / ".env"

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()

        if key:
            os.environ.setdefault(key, value)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(f"{name} is missing from the environment.")

    return value


def _ids(name: str) -> tuple[str, ...]:
    values = tuple(
        item.strip()
        for item in _required(name).replace(";", ",").split(",")
        if item.strip()
    )

    if not values:
        raise ValueError(
            f"{name} must contain at least one Notion user ID."
        )

    return values


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(
        name,
        "true" if default else "false",
    ).strip().lower()

    if raw in {"1", "true", "yes", "on"}:
        return True

    if raw in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"{name} must be true or false.")


@dataclass(frozen=True)
class Settings:
    notion_api_key: str
    notion_version: str
    notion_timeout_seconds: int

    database_url: str

    buffer_api_key: str
    buffer_channel_id: str  # Threads channel (existing setting)
    buffer_channel_id_x: str
    buffer_organization_id: str | None
    buffer_timeout_seconds: int

    publishing_platform: str
    publishing_api_token: str
    enable_live_scheduling: bool

    authorized_approver_ids: tuple[str, ...]
    authorized_founder_approver_ids: tuple[str, ...]

    approval_snapshot_secret: str
    slack_webhook_url: str | None


def get_settings() -> Settings:
    """Load and validate application configuration."""
    load_env_file()

    notion_timeout = int(
        os.getenv("NOTION_TIMEOUT_SECONDS", "20")
    )

    buffer_timeout = int(
        os.getenv("BUFFER_TIMEOUT_SECONDS", "20")
    )

    if not 1 <= notion_timeout <= 120:
        raise ValueError(
            "NOTION_TIMEOUT_SECONDS must be between 1 and 120."
        )

    if not 1 <= buffer_timeout <= 120:
        raise ValueError(
            "BUFFER_TIMEOUT_SECONDS must be between 1 and 120."
        )

    secret = _required("APPROVAL_SNAPSHOT_SECRET")

    if len(secret.encode("utf-8")) < 32:
        raise ValueError(
            "APPROVAL_SNAPSHOT_SECRET must contain at least 32 bytes."
        )

    platform = _required("PUBLISHING_PLATFORM").lower()

    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            "PUBLISHING_PLATFORM must be one of: "
            + ", ".join(sorted(SUPPORTED_PLATFORMS))
        )

    return Settings(
        notion_api_key=_required("NOTION_API_KEY"),
        notion_version=os.getenv(
            "NOTION_VERSION",
            "2026-03-11",
        ).strip(),
        notion_timeout_seconds=notion_timeout,

        database_url=_required("DATABASE_URL"),

        buffer_api_key=_required("BUFFER_API_KEY"),
        buffer_channel_id=_required("BUFFER_CHANNEL_ID"),
        buffer_channel_id_x=_required("BUFFER_CHANNEL_ID_X"),
        buffer_organization_id=(
            os.getenv("BUFFER_ORGANIZATION_ID", "").strip() or None
        ),
        buffer_timeout_seconds=buffer_timeout,

        publishing_platform=platform,
        publishing_api_token=_required("PUBLISHING_API_TOKEN"),
        enable_live_scheduling=_bool(
            "ENABLE_LIVE_SCHEDULING",
            False,
        ),

        authorized_approver_ids=_ids(
            "NOTION_AUTHORIZED_APPROVER_IDS"
        ),
        authorized_founder_approver_ids=_ids(
            "NOTION_AUTHORIZED_FOUNDER_APPROVER_IDS"
        ),

        approval_snapshot_secret=secret,
        slack_webhook_url=(
            os.getenv("SLACK_WEBHOOK_URL", "").strip() or None
        ),
    )