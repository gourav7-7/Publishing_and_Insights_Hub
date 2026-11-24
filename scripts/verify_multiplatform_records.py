"""Read-only verification of persisted multi-platform job/run metadata.

Loads the existing project .env without printing credentials or channel IDs.
Only SELECT statements are executed; no Buffer, Notion, or Slack calls occur.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_env_file  # noqa: E402
from app.database import Database  # noqa: E402


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is missing from the local environment (.env).")
    return value


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    expected = {
        "threads": ("Buffer", _required("BUFFER_CHANNEL_ID"), "BUFFER_CHANNEL_ID"),
        "x": ("Buffer", _required("BUFFER_CHANNEL_ID_X"), "BUFFER_CHANNEL_ID_X"),
        "linkedin": ("Simulation", "simulation:linkedin", "simulation:linkedin"),
    }
    database = Database(_required("DATABASE_URL"))

    query = """
        SELECT
            j.id,
            j.platform,
            j.provider,
            j.channel_id,
            j.status,
            j.provider_post_id,
            wr.provider AS workflow_provider
        FROM publishing_jobs AS j
        LEFT JOIN LATERAL (
            SELECT provider
            FROM workflow_runs
            WHERE job_id = j.id
            ORDER BY started_at DESC
            LIMIT 1
        ) AS wr ON TRUE
        WHERE j.platform IN ('threads', 'x', 'linkedin')
        ORDER BY j.created_at DESC
        LIMIT 50
    """

    with database._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(query)
            rows: list[dict[str, Any]] = [dict(row) for row in cur.fetchall()]

    if not rows:
        print("No Threads, X, or LinkedIn rows found in publishing_jobs.")
        print("No persisted multi-platform records were verified.")
        return 2

    failures = 0
    for row in rows:
        platform = str(row.get("platform") or "").lower()
        provider, expected_channel, channel_label = expected[platform]
        checks = {
            "job_provider": row.get("provider") == provider,
            "channel": row.get("channel_id") == expected_channel,
            "workflow_provider": row.get("workflow_provider") == provider,
            "scheduled_id": (
                row.get("status") != "scheduled" or bool(row.get("provider_post_id"))
            ),
        }
        passed = all(checks.values())
        failures += 0 if passed else 1
        marker = "PASS" if passed else "FAIL"
        print(
            f"[{marker}] platform={platform} provider={row.get('provider')} "
            f"channel={channel_label} status={row.get('status')} "
            f"provider_post_id={'present' if row.get('provider_post_id') else 'missing'} "
            f"workflow_provider={row.get('workflow_provider')} "
            f"checks={sum(checks.values())}/{len(checks)}"
        )

    print(f"Rows checked: {len(rows)}; failed rows: {failures}")
    print("Channel identifiers and credential values were not printed.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"VERIFICATION ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
