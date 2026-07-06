# Meta → Notion Hook-Rate & Ad Library Sync

Pulls performance data from Meta (Facebook/Instagram) Business Manager and
writes hook rate + Ad Library links back onto a Notion creative board. Runs
unattended on GitHub Actions — no server, no hosting.

See [`../../BUILD_BRIEF.md`](../../BUILD_BRIEF.md) for the full spec and
design rationale. This README is the operator setup guide.

## What it does

For every row on the Notion board:

1. Matches the row to a Meta ad by **ad name**, read from the **Ad Name** property (falls back to the page Title if that property doesn't exist or is empty — see `PROP_AD_NAME`).
2. If lifetime spend > £100 (configurable), computes
   **hook rate = 3-second video views ÷ impressions × 100**.
3. Builds the Facebook Ad Library deep link for that ad (falls back to a
   page-level link if an exact match isn't available).
4. Writes **Hook Rate** and **Ad Link**, and sets **Sync Status** depending on the outcome:
   - **`Synced`** — hook rate was actually computed and written.
   - **`Duplicate Ad Name`** — the Ad Name matches more than one Meta ad; see the `WARNING` line in the log for which `ad_id`s collide.
   - **`Spend below £100`** — matched a Meta ad, but lifetime spend hasn't crossed the threshold yet.
   - *(left unchanged)* — matched, spend is above threshold, but there's no video view data (e.g. a static/image ad) — nothing to mark Synced for, and no other status applies, so it's simply not touched.

Rows already marked `Synced` are skipped on subsequent runs unless `FORCE_REPROCESS=true`. Rows marked `Duplicate Ad Name` or `Spend below £100` are **not** skipped — they're re-evaluated every run, since spend can cross the threshold or a duplicate can get fixed later.

## One-time setup

### 1. Notion

1. Create an internal integration at <https://www.notion.so/my-integrations> and copy its token.
2. Open the target database → **⋯ → Connections → Add connections** → add the integration. (Required — without this, writes 404.)
3. Make sure these columns exist (the script never creates/changes schema):
   - **Ad Name** — the value matched against the Meta ad name. Title, rich text, select, or formula (string/number/boolean) all work. If missing or blank on a row, the page Title is used instead.
   - **Hook Rate** — Number. If you want it to display with a `%` sign, set its format to **Percent** in the column settings — the script writes the fraction (e.g. `0.4156` for 41.56%) specifically so Percent-formatted columns display correctly. If left as plain Number, it'll show the raw fraction, not the percentage.
   - **Ad Link** — URL
   - **Sync Status** — Select, Status, or Checkbox. Values written: `Synced`, `Duplicate Ad Name`, `Spend below £100`. **If this is a Status property** (not Select), the API cannot create new options on the fly — you must manually add `Duplicate Ad Name` and `Spend below £100` as valid options in the property's settings first, or those writes will fail. Select properties don't have this restriction. Checkbox can only represent `Synced` (checked) vs. everything else (unchecked) — it can't distinguish duplicate from below-threshold.
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
| `PROP_AD_NAME` | `Ad Name` | Property matched against the Meta ad name (title/rich_text/select/formula). Falls back to the page Title if missing/empty. |
| `TEST_AD_NAME` | *(unset)* | If set, only the one Notion row whose match name (per `PROP_AD_NAME`) exactly matches this is processed — everything else is skipped. Handy for testing a single ad before running against the whole board. |
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

- **HTTP 403 `Ad account owner has NOT grant ads_management or ads_read permission`** — two possible causes:
  1. The System User isn't assigned to the ad account as an asset (token scope alone isn't enough). Business Settings → Users → System Users → select the user → **Assign Assets** → toggle on the ad account with at least "View performance" access.
  2. The `META_AD_ACCOUNT_ID` secret doesn't match an account the token can actually see. Check with `GET /v21.0/me/adaccounts?fields=name,account_id&access_token=<token>` — the id in the secret must match one of the `account_id` values returned (no `act_` prefix needed in the secret; the script adds it).
- **HTTP 400 `video_3_sec_watched_actions is not valid for fields param`** — Meta has removed this field from current Graph API versions; requesting it fails the whole insights call. Already fixed in the deployed script (it no longer requests that field and uses the `actions`/`video_view` fallback instead). If you see this, you're running an older copy of `sync_hook_rate.py` — pull the latest.
- **404 on Notion writes** — the integration isn't connected to the database (see setup step 1.2).
- **`TEST_AD_NAME` filters to 0 rows even though the ad exists** — the match value isn't coming from where you think. By default the script reads the **Ad Name** property (title/rich_text/select/formula), not the page Title, unless that property is missing/empty. If your board keeps the matchable name somewhere else, set `PROP_AD_NAME` to that column's name.
- **No Meta ad matches '<name>'** — the matched Notion value (see `PROP_AD_NAME` above) doesn't exactly equal a Meta ad name (whitespace/case differences are normalized automatically, everything else isn't).
- **matches multiple ads by name; skipping** — two+ Meta ads share a name, which shouldn't happen under the naming convention; a `WARNING` line right before it lists the colliding `ad_id`s so you can find and rename the duplicate in Meta. Don't rename it by editing the Notion side to "make it unique" instead — the Notion **Ad Name** value and the Meta ad name must stay identical, or matching breaks for that row entirely.
- **Ad Library link is the page-level fallback, not exact** — either Ad Library API identity/location confirmation isn't complete yet, or creative body text didn't match closely enough. Not an error; link still works.
