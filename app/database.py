"""Small PostgreSQL store for idempotency and per-request execution logs."""
from __future__ import annotations

import json
import re
from typing import Any


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS publishing_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        idempotency_key CHAR(64) NOT NULL UNIQUE,
        notion_page_id TEXT NOT NULL,
        content_type TEXT NOT NULL CHECK (content_type IN ('brand','founder')),
        platform TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'Buffer',
        status TEXT NOT NULL CHECK (status IN ('submitting','scheduled','draft','failed','unknown')),
        approval_snapshot_hash TEXT NOT NULL,
        scheduled_at TIMESTAMPTZ NOT NULL,
        provider_post_id TEXT,
        provider_status TEXT,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_runs (
        run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id UUID REFERENCES publishing_jobs(id) ON DELETE SET NULL,
        content_id TEXT NOT NULL,
        source TEXT NOT NULL,
        inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
        outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
        approval_details JSONB NOT NULL DEFAULT '{}'::jsonb,
        provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','succeeded','needs_attention','failed','unknown')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        error TEXT,
        model_provider TEXT,
        model_name TEXT,
        input_tokens BIGINT,
        output_tokens BIGINT,
        estimated_cost NUMERIC(12,6)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_workflow_runs_content_started ON workflow_runs(content_id, started_at DESC)",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_publishing_active_page_channel_v2
       ON publishing_jobs(notion_page_id, channel_id)
       WHERE status IN ('submitting','scheduled','unknown','draft')""",
)


class Database:
    def __init__(self, database_url: str):
        if not database_url.strip():
            raise ValueError("DATABASE_URL is required.")
        self.database_url = database_url

    def _connect(self):
        # Lazy import keeps unit tests for the publishing service independent
        # of the optional PostgreSQL driver; production installs requirements.txt.
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL driver is missing. Install dependencies with pip install -r requirements.txt."
            ) from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def initialize_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in _SCHEMA:
                    cur.execute(statement)
                # The earlier project version did not store a platform column.
                # Add it safely without deleting existing jobs or execution logs.
                cur.execute(
                    "ALTER TABLE publishing_jobs ADD COLUMN IF NOT EXISTS "
                    "platform TEXT NOT NULL DEFAULT 'unknown'"
                )

    def ping(self) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                return cur.fetchone()["ok"] == 1

    @staticmethod
    def redact(value: str | None) -> str | None:
        if value is None:
            return None
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
        value = re.sub(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            value,
        )
        value = re.sub(r"https?://hooks\.slack\.com/services/[^\s,;]+", "[REDACTED_SLACK_WEBHOOK]", value)
        return value[:2000]

    @classmethod
    def _safe_json(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.redact(value)
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if re.search(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|webhook)", key_text):
                    safe[key_text] = "[REDACTED]"
                else:
                    safe[key_text] = cls._safe_json(item)
            return safe
        if isinstance(value, (list, tuple)):
            return [cls._safe_json(item) for item in value]
        return value

    def reserve_job(
        self,
        *,
        idempotency_key: str,
        notion_page_id: str,
        content_type: str,
        platform: str,
        channel_id: str,
        approval_snapshot_hash: str,
        scheduled_at: str,
        provider: str = "Buffer",
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically reserve this page/channel before provider scheduling."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO publishing_jobs (
                           idempotency_key, notion_page_id, content_type, platform,
                           channel_id, provider, approval_snapshot_hash, scheduled_at, status
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'submitting')
                       ON CONFLICT DO NOTHING RETURNING *""",
                    (
                        idempotency_key, notion_page_id, content_type, platform,
                        channel_id, provider, approval_snapshot_hash, scheduled_at,
                    ),
                )
                created = cur.fetchone()
                if created:
                    return True, dict(created)

                cur.execute(
                    "SELECT * FROM publishing_jobs WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    return False, dict(existing)

                cur.execute(
                    """SELECT * FROM publishing_jobs
                       WHERE notion_page_id=%s AND channel_id=%s
                         AND status IN ('submitting','scheduled','unknown','draft')
                       ORDER BY created_at DESC LIMIT 1""",
                    (notion_page_id, channel_id),
                )
                existing = cur.fetchone()
                if existing:
                    return False, dict(existing)
                raise RuntimeError("Publishing reservation conflicted but no existing job was found.")

    def reopen_failed_job(self, job_id: str) -> bool:
        """Only a definitively failed job may be explicitly retried."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE publishing_jobs
                       SET status='submitting', last_error=NULL, updated_at=NOW()
                       WHERE id=%s AND status='failed'""",
                    (job_id,),
                )
                return cur.rowcount == 1

    def mark_scheduled(
        self,
        job_id: str,
        provider_post_id: str,
        provider_status: str | None,
        scheduled_at: str,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE publishing_jobs
                       SET status='scheduled', provider_post_id=%s, provider_status=%s,
                           scheduled_at=%s, last_error=NULL, updated_at=NOW()
                       WHERE id=%s""",
                    (provider_post_id, provider_status, scheduled_at, job_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Publishing job disappeared before scheduled state was saved.")

    def mark_failed(self, job_id: str, reason: str) -> None:
        self._mark_status(job_id, "failed", reason)

    def mark_unknown(self, job_id: str, reason: str) -> None:
        self._mark_status(job_id, "unknown", reason)

    def _mark_status(self, job_id: str, status: str, reason: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE publishing_jobs
                       SET status=%s, last_error=%s, updated_at=NOW()
                       WHERE id=%s""",
                    (status, self.redact(reason), job_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Publishing job was not found while updating its status.")

    def start_execution(
        self,
        *,
        content_id: str,
        source: str,
        inputs: dict[str, Any],
        provider: str = "Buffer",
    ) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO workflow_runs(content_id,source,inputs,provider,status)
                       VALUES (%s,%s,%s::jsonb,%s,'running') RETURNING run_id""",
                    (content_id, source, json.dumps(self._safe_json(inputs)), provider),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Workflow run could not be recorded.")
                return str(row["run_id"])

    def finish_execution(
        self,
        run_id: str,
        *,
        status: str,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        job_id: str | None = None,
        content_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        approval_details: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"succeeded", "needs_attention", "failed", "unknown"}:
            raise ValueError("Invalid workflow run status.")
        safe_inputs = json.dumps(self._safe_json(inputs)) if inputs is not None else None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE workflow_runs SET
                           job_id=COALESCE(%s,job_id),
                           content_id=COALESCE(%s,content_id),
                           inputs=COALESCE(%s::jsonb,inputs),
                           outputs=%s::jsonb,
                           approval_details=%s::jsonb,
                           status=%s, completed_at=NOW(), error=%s
                       WHERE run_id=%s""",
                    (
                        job_id, content_id, safe_inputs,
                        json.dumps(self._safe_json(outputs or {})),
                        json.dumps(self._safe_json(approval_details or {})),
                        status, self.redact(error), run_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Workflow run was not found when completing execution.")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM publishing_jobs WHERE id=%s", (job_id,))
                row = cur.fetchone()
                return dict(row) if row else None
