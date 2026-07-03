# Meta → Notion Hook-Rate & Ad Library Sync

Pulls performance data from Meta (Facebook/Instagram) Business Manager and
writes hook rate + Ad Library links back onto a Notion creative board. Runs
unattended on GitHub Actions — no server, no hosting.

See [`../../BUILD_BRIEF.md`](../../BUILD_BRIEF.md) for the full spec and
design rationale. This README is the operator setup guide.

## What it does

For every row on the Notion board:

1. Matches the row to a Meta ad by **ad name**.
2. If lifetime spend > £100 (configurable), computes
   **hook rate = 3-second video views ÷ impressions × 100**.
3. Builds the Facebook Ad Library deep link for that ad (falls back to a
   page-level link if an exact match isn't available).
4. Writes **Hook Rate**, **Ad Link**, and marks **Sync Status = Synced**.

Already-synced rows are skipped on subsequent runs unless `FORCE_REPROCESS=true`.

## One-time setup

### 1. Notion

1. Create an internal integration at <https://www.notion.so/my-integrations> and copy its token.
2. Open the target database → **⋯ → Connections → Add connections** → add the integration. (Required — without this, writes 404.)
3. Make sure these columns exist (the script never creates/changes schema):
   - **Hook Rate** — Number
   - **Ad Link** — URL
   - **Sync Status** — Select, Status, or Checkbox (value written: `Synced`)
   - *(optional)* **Spend** — Number, only written if `WRITE_SPEND=true`
4. Copy the **database id** — the 32-hex-char segment in the database URL.

### 2. Meta

1. Business Settings → Users → **System Users** → create one (or use an existing one) → **Generate New Token** with the **`ads_read`** permission, scoped to the target ad account. Use a long-lived / no-expiry system user token.
2. Note the **ad account id** (the digits after `act_` in Ads Manager's URL — the `act_` prefix is added automatically if you paste it without one).
3. Confirm the account currency is GBP: `GET /v21.0/act_<ID>?fields=currency`. If it isn't, the £100 threshold comparison needs an FX step (not built — see BUILD_BRIEF.md §12).
4. For **exact** per-ad Ad Library links, complete Meta's **identity + location confirmation** for Ad Library API access (Business Settings → Security Center / Business verification). Until that's done, links degrade gracefully to the page-level Ad Library view — nothing breaks.

### 3. GitHub repo

Add repo **secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | Value |
|---|---|
| `NOTION_TOKEN` | Notion integration token from step 1 |
| `NOTION_DATABASE_ID` | Database id from step 1 |
| `META_ACCESS_TOKEN` | System user token from step 2 |
| `META_AD_ACCOUNT_ID` | Ad account id from step 2 |

Optional tuning goes in repo **variables** (same page, Variables tab) — see
the table below. Leave unset to use defaults.

## Local testing

```bash
cd automation/hookrate
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the 4 required values; DRY_RUN=true by default
python3 sync_hook_rate.py
```

Dry run logs every intended write (hook rate, Ad Library URL, spend) without
touching Notion. Confirm the numbers look right — especially which 3-second
view field got used (`video_3_sec_watched_actions` vs `video_view` action vs
`video_play_actions` fallback; it's logged when the fallback is used) — before
flipping to a live run with `DRY_RUN=false`.

## Configuration reference

All configuration is environment variables (`.env` locally, GitHub
secrets/variables in CI).

**Required:**

| Var | Meaning |
|---|---|
| `NOTION_TOKEN` | Notion internal integration token |
| `NOTION_DATABASE_ID` | Target database id |
| `META_ACCESS_TOKEN` | Long-lived System User token, `ads_read` scope |
| `META_AD_ACCOUNT_ID` | `act_<id>` (prefix auto-added if omitted) |

**Optional:**

| Var | Default | Meaning |
|---|---|---|
| `SPEND_THRESHOLD_GBP` | `100` | Min lifetime spend before a row is processed |
| `DRY_RUN` | `false` | Compute + log intended writes, no Notion changes |
| `FORCE_REPROCESS` | `false` | Re-process rows already marked `Synced` |
| `META_DATE_PRESET` | `maximum` | Insights window (`maximum` = lifetime, or e.g. `last_30d`) |
| `AD_LIBRARY_COUNTRY` | `GB` | Country for Ad Library search + fallback link |
| `WRITE_SPEND` | `false` | Also write spend into a `Spend` column if present |
| `FAIL_ON_ERROR` | `false` | Make the CI run red if any row errored |
| `PROP_HOOK_RATE` | `Hook Rate` | Notion column name override |
| `PROP_AD_LIBRARY` | `Ad Link` | Notion column name override |
| `TEST_AD_NAME` | *(unset)* | If set, only the one Notion row whose title exactly matches this is processed — everything else is skipped. Handy for testing a single ad before running against the whole board. |
| `PROP_STATUS` | `Sync Status` | Notion column name override |
| `PROP_SPEND` | `Spend` | Notion column name override |

## Running in CI

The workflow (`.github/workflows/hook-rate-sync.yml`) runs daily at 07:00 UTC
and can be triggered manually from the Actions tab (**Run workflow**), which
defaults to a dry run. Scheduled runs always go live.

Recommended verification order before trusting the daily cron:

1. `workflow_dispatch` with `dry_run=true` → inspect the logs.
2. `workflow_dispatch` with `dry_run=false` against a scratch/test database → confirm the Number, URL, and Status land correctly.
3. Spot-check one ad's hook rate against Business Manager directly.
4. Let the daily schedule take over.

## Troubleshooting

- **HTTP 403 `Ad account owner has NOT grant ads_management or ads_read permission`** — the token's `ads_read` scope isn't the issue; the System User isn't assigned to the ad account as an asset. Business Settings → Users → System Users → select the user → **Assign Assets** → toggle on the ad account with at least "View performance" access. No need to regenerate the token.
- **404 on Notion writes** — the integration isn't connected to the database (see setup step 1.2).
- **No Meta ad matches '<title>'** — the Notion row title doesn't exactly match a Meta ad name (whitespace/case differences are normalized automatically, everything else isn't).
- **matches multiple ads by name; skipping** — two+ Meta ads share a name; rename one or add an authoritative ad-id column (see BUILD_BRIEF.md §12).
- **Ad Library link is the page-level fallback, not exact** — either Ad Library API identity/location confirmation isn't complete yet, or creative body text didn't match closely enough. Not an error; link still works.
