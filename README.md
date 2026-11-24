# Just Sortd — Publishing & Insights Hub (take-home MVP)

## What this project proves

A human-approved Notion item is validated and routed to one selected destination. **Threads and X use separate Buffer channels; LinkedIn uses a local Simulation provider and never creates or queries a Buffer post.** The API is a thin bridge; Notion remains the source of truth and there is no custom front-end.

This is intentionally a narrow take-home MVP, not the complete long-term system for 200 posts/month. It does not create content, use an AI model, create drafts, support the full PRD platform matrix, or generate performance summaries.

## Simple architecture

```text
Notion: Content (Brand) / Founder Content
        |
        | POST /api/v1/publishing/jobs
        v
FastAPI: authenticate caller + validate fresh Notion approval
        |
        +--> PostgreSQL: atomic job reservation + workflow run log
        |
        +--> Buffer: schedule approved text on Threads or X
        |    OR
        +--> Simulation: stable LinkedIn simulation ID (no Buffer call)
        |
        +--> Notion: Provider, Provider Post ID, Scheduled status, time, URL
        |
        +--> Slack: plain-language failure + recovery instructions
```

Core source files are grouped by responsibility:

| File | Responsibility |
|---|---|
| `app/main.py` | The API and health endpoints |
| `app/config.py` | One settings loader; no phase-specific settings |
| `app/content.py` | Read and validate Notion content for the selected platform |
| `app/approval.py` | Allowlisted human approval and signed content snapshot |
| `app/approval_actions.py` | Sign an approval already entered by a human in Notion |
| `app/publishing.py` | Shared Buffer/Simulation workflow, idempotency and Notion write-back |
| `app/notion_client.py` | Minimal Notion page read/update client |
| `app/buffer_client.py` | Buffer scheduling and read/reconciliation client |
| `app/database.py` | PostgreSQL job lock and workflow-run log |
| `app/slack.py` | Central best-effort Slack alerts |

There is no phase-named code, draft route, database/view setup framework, or large diagnostics folder.

## What is implemented

- Supports the **brand** and **founder** Notion content sources using the same property contract.
- Requires `Approval Status = Approved`, exactly one allowlisted approver, a valid approval timestamp, and a valid signed snapshot of the publishable content.
- Requires separate founder sign-off when `Founder Approval Required` is checked on founder content.
- Requires exactly one selected target platform: `Threads`, `X`, or `LinkedIn`.
- Re-reads and re-validates the page immediately before the provider action.
- Routes Threads to `BUFFER_CHANNEL_ID` and X to `BUFFER_CHANNEL_ID_X`; each uses Buffer for scheduling.
- Routes LinkedIn to provider `Simulation`, creates a stable synthetic post ID from the reserved PostgreSQL job, and makes no Buffer API call.
- Saves the provider post ID before trying to update Notion. Replay reuses the stored ID; Buffer-backed posts are read for reconciliation, while LinkedIn simulation does not query Buffer.
- Stores the selected platform, channel identifier and provider (`Buffer` or `Simulation`) with the job and provider with the workflow run.
- Quarantines uncertain Buffer outcomes as `unknown`; automatic retry is blocked until a human reconciles Buffer.
- Logs each API execution, inputs, outputs, timestamps, approval context, status and errors. Model/token/cost values remain NULL because this system does not use AI.
- Sends Slack alerts for authentication, scheduling, and synchronization failures. Slack failure never changes publishing state.

## Not in this take-home demo

The full PRD platform matrix, media upload, automatic post-status polling, engagement ingestion, weekly insights, vendor/partner dashboards, and automated retry workers. The build plan below explains how these would be added without duplicating the publishing layer.

## Setup

### 1. Create the Notion properties

Create or reuse the two source databases required by the PRD:

- `Content (Brand)`
- `Founder Content`

Share both databases with the Notion integration. Each database needs these properties (names and types must match):

| Property | Notion type | Purpose |
|---|---|---|
| `Title` | Title | Content name |
| `Content ID` | Rich text | Stable content identifier |
| `Source` | Rich text | Origin / partner |
| `Caption` | Rich text | Approved text to publish |
| `Assets / Media URLs` | Rich text | Must be empty for this text-only demo |
| `Target Platforms` | Multi-select | Exactly one selected target for this demo |
| `Scheduled Time` | Date | Future date and time with timezone |
| `Publishing Status` | Select | `Draft`, `Ready`, `Scheduled`, `Published`, `Failed`, `Needs Reconciliation` |
| `Approval Status` | Select | `Pending`, `Approved`, `Rejected` |
| `Approver` | People | Exactly one primary approver |
| `Approval Timestamp` | Date | Time of the human approval |
| `Approval Snapshot Hash` | Rich text | HMAC signature written by the helper after approval |
| `Founder Approval Required` | Checkbox | Used on founder content |
| `Founder Approval Status` | Select | `Pending`, `Approved`, `Rejected` |
| `Founder Approver` | People | Authorized founder approver |
| `Founder Approval Timestamp` | Date | Founder sign-off time |
| `Provider` | Select | `Buffer`, `Simulation` |
| `Provider Post ID` | Rich text | Provider's post ID |
| `Published URL` | URL | Populated when Buffer reports Published; remains empty for LinkedIn simulation |
| `Failure Reason` | Rich text | Failure details, cleared after successful sync |

`Publishing Status` starts as `Ready` only after the team has finished content review. The separate approval properties are still checked by the service; a `Ready` status alone is never accepted as approval.

### 2. Select a supported platform

This MVP currently accepts exactly one `Target Platforms` value per request:

| API `platform` value | Notion target value | Provider | Channel configuration |
|---|---|---|---|
| `threads` | `Threads` | `Buffer` | `BUFFER_CHANNEL_ID` |
| `x` | `X` | `Buffer` | `BUFFER_CHANNEL_ID_X` |
| `linkedin` | `LinkedIn` | `Simulation` | Fixed internal key `simulation:linkedin` |

Set `PUBLISHING_PLATFORM` to `threads`, `x`, or `linkedin` for the default when the request omits `platform`. An explicit API `platform` value overrides that default. LinkedIn simulation does not call Buffer and must not be described as a real LinkedIn publication. The Notion `Provider` select must include both `Buffer` and `Simulation` options.

### 3. Configure and install

```powershell
Copy-Item .env.example .env
# Edit .env with your real credentials and IDs.
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`DATABASE_URL` points to PostgreSQL. The application creates the two small tables it needs (`publishing_jobs` and `workflow_runs`) at startup. Existing databases from the previous project can be reused; old event and draft records are not deleted.
Before first startup, ensure PostgreSQL is running, the database specified in
`DATABASE_URL` already exists, and the configured database role has permission
to create and use tables. The application creates its tables but does not
create the PostgreSQL server, database role, or database.

### 4. Sign the human approval snapshot

A human first updates the approval fields in Notion. Then run:

```powershell
python -m scripts.sign_approval --page-id YOUR_NOTION_PAGE_UUID --content-type brand
```

For founder content use `--content-type founder`. The script does **not** set the Approved status or impersonate an approver; it signs the exact state already recorded in Notion and verifies the allowlist and founder gate.

For a production workflow, connect the Notion approval action to this same signing function through a trusted automation so the person approving does not need to run a script.

### 5. Run locally

Keep live scheduling disabled until the Notion test page and the selected destination are verified. Then enable it only for the controlled demo.

```powershell
# Set ENABLE_LIVE_SCHEDULING=true in .env only for the test run.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Health checks:

- `GET http://127.0.0.1:8000/health/live`
- `GET http://127.0.0.1:8000/health/ready`

### 6. Schedule one approved test item

```powershell
$headers = @{ "X-Publishing-Token" = "YOUR_PUBLISHING_API_TOKEN" }
$body = @{ page_id = "YOUR_NOTION_PAGE_UUID"; content_type = "brand"; platform = "threads" } | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/publishing/jobs" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

For `threads` and `x`, the first request should return `status=scheduled`, a Buffer `provider_post_id`, `notion_updated=true`, and `duplicate_request=false`. Notion should show `Provider=Buffer`, the provider post ID, `Publishing Status=Scheduled`, the scheduled time, and an empty failure reason. For `linkedin`, Notion should show `Provider=Simulation`; the returned synthetic ID is stored in PostgreSQL and no Buffer post is created.

Repeat the identical request to demonstrate idempotency. Threads/X replay reads the existing Buffer post and must not submit a second scheduling mutation. LinkedIn replay reuses the same synthetic ID and makes no Buffer request.

## Failure and recovery rules

| Failure | Stored state | Operator action |
|---|---|---|
| Invalid API key / 401 or 403 | Definitive failure if Buffer rejected a write; no mutation retry | Fix credentials or permissions, then repeat the same request |
| Buffer explicitly rejects a request | `failed` | Correct the channel/request issue, then retry the same approved page |
| Buffer timeout or uncertain response after mutation begins | `unknown` | Do not retry. Search the selected Buffer channel and reconcile the provider post first |
| Buffer succeeded; Notion update failed | Job remains `scheduled` with provider ID | Restore Notion access and replay the same request; it reads the saved Buffer post and repairs Notion without creating another post |
| LinkedIn simulation; Notion update failed | Job remains `scheduled` with synthetic provider ID | Restore Notion access and replay the same request; it reuses the same synthetic ID and does not call Buffer |
| Slack webhook unavailable | Publishing state unchanged | Fix Slack later; inspect the persisted workflow run |

## Tests

```powershell
python -m pytest -q
python -m compileall -q app scripts
git diff --check
```

All network-facing integrations are mocked in unit tests. A real end-to-end demo still requires your Notion, PostgreSQL, Buffer and Slack credentials.

---

# One-page build plan (PRD 2)

## V1: ship the shared handoff first

Start with one approved text item from either Notion content database, one target platform, one test account, exact scheduling time, and status write-back. This proves the riskiest contract: the source of truth, approval gate, external scheduling mutation, provider ID persistence, and Notion reconciliation. Keep the API as a thin bridge; use Notion and Slack as the UI.

## Architecture and data model

Notion remains the content and approval source. A single authenticated FastAPI endpoint accepts only a Notion page ID and content type; it never accepts caller-supplied captions or schedules. The service validates primary/founder approval and a signed snapshot, reserves the page/channel in PostgreSQL, schedules through one provider adapter, stores the provider ID, then writes the actual provider status back to Notion. Slack receives actionable failures.

`publishing_jobs` stores idempotency key, Notion page/content type, platform/channel, approval snapshot hash, schedule time, job status, provider ID/status and last error. `workflow_runs` stores each execution's inputs, outputs, provider, approval context, start/completion time, status and error. Model, token and AI cost fields remain empty because no model is used in PRD 2's publishing path.

## Buy vs. build

- **Buy/use:** Notion for content, approval and schedule view; Slack for alerts; an existing scheduler/provider for publishing; PostgreSQL for durable job/run records.
- **Build:** only the approval-aware handoff, idempotency guard, provider adapter, status reconciliation and lightweight run log.
- **Do not build:** a custom content editor, AI caption generator, separate scheduler for every content source, or a custom dashboard.

The PRD specifically says Treg is already in use for X, LinkedIn and Substack, so it should be evaluated before expanding the Buffer adapter. Its public social page describes connected-account posting for Instagram, LinkedIn, YouTube and X, while its current catalog presents Postiz-backed posting entries for X, Instagram, LinkedIn and YouTube. Substack is not listed in those public social-publishing entries. Before adopting it as the shared scheduler, verify Substack support, scheduled-time semantics (not only immediate posting), status/read-back, idempotency or lookup after timeouts, media constraints, quotas and actual account pricing. See https://treg.io/social and https://treg.to/.

## Failure handling

Authentication failures are definitive and alert the operator to restore access. Explicit provider rejections become `failed` and can be retried after correction. A timeout after a mutation begins becomes `unknown` and is never automatically retried. A successful provider write is persisted before Notion sync, so a Notion failure can be repaired by replaying the same request without a duplicate. Slack alerts state what happened, recovery steps and whether retry is safe.

## Roadmap after the demo

1. Evaluate Treg against the full platform matrix; keep one common scheduling interface and use the existing vendor wherever its capabilities are sufficient.
2. Add provider-specific media handling and per-platform formatting rules.
3. Poll provider lifecycle/status and sync live URL/status to Notion.
4. Ingest available post metrics and deliver a weekly Notion/Slack summary of what worked and what did not.
5. Add external partner due/late/performance views using Notion database views, not a custom dashboard.

## Indicative cost

For the local take-home demo, incremental app hosting cost is **₹0**; it runs on the developer machine and uses the team's existing Notion, Slack and scheduler accounts. For a small always-on pilot, budget approximately **₹1,000–₹2,000/month** for a small API host and managed PostgreSQL, excluding existing SaaS subscriptions, social-provider charges and taxes. No LLM/API cost is incurred because this workflow does not use a model. Confirm the Treg/Buffer plan and per-post charges before rollout; the public Treg pages show different publishing/catalog routes, so account-specific billing should be checked rather than assumed.
