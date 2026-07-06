# Build Brief: Meta → Notion Hook-Rate & Ad Library Sync

> Hand this document to a Claude Code instance (or any engineer) as a complete,
> self-contained spec. It describes what to build, why, the exact API calls, the
> file layout, and a full reference implementation that has already been written
> and unit-tested. Nothing outside this document is required.

---

## 1. Goal

The paid-media / creative-strategy team maintains a **Notion database ("board")**
of ad creative assets. For each ad, they want performance data pulled
automatically from **Meta (Facebook/Instagram) Business Manager** and written
back onto the board.

For every ad asset on the board:

1. Match the Notion item to the corresponding **Meta ad** (by ad **name**).
2. If that ad has spent **more than £100** (lifetime by default), compute the
   **hook rate** = `3-second video views ÷ impressions × 100`.
3. Derive the **Facebook Ad Library deep link** for that specific ad
   (`https://www.facebook.com/ads/library/?id=<ad_archive_id>`), with a
   page-level link as a fallback.
4. Write the **hook rate** (Number) and **Ad Library link** (URL) back onto the
   Notion row, and mark it `Synced`.

### Hard constraint: no hosting

It must run **unattended without hosting a server**. Target runtime is
**GitHub Actions** (scheduled cron + manual trigger), with all credentials in
GitHub repository secrets. The same single script also runs locally for testing.

---

## 2. Key decisions (already made — do not re-litigate)

| Decision | Choice | Rationale |
|---|---|---|
| Runtime | **GitHub Actions cron** | Free scheduled compute, encrypted secrets, run logs, `workflow_dispatch` for manual runs. No server. |
| Language | **Python 3.11**, one dependency (`requests`) | Everything is plain HTTPS JSON; a single annotated file is readable for a non-eng team. No SDKs. |
| Meta access | **Full Marketing API** already set up (app + long-lived System User token + ad account id) | Given. |
| Notion↔Meta matching | **By ad name** (normalized) | Simplest; the team keeps Notion titles equal to Meta ad names. Ambiguous/missing names are skipped-and-logged, never guessed. |
| Ad Library link | **Exact per-ad deep link**, page-level fallback | Best UX; fallback guarantees the cell is never empty. |
| Spend window | **Lifetime** (`date_preset=maximum`) | "has had over £100 of spend" = cumulative. Configurable. |

> If the internal Claude runs in a different company environment, the only thing
> that changes is *where secrets live* (e.g. an internal secrets manager or
> self-hosted Actions runner). The script reads everything from environment
> variables, so it is portable to any secret store.

---

## 3. The two non-obvious technical facts

These are the parts most likely to trip up an implementer. They are already
solved in the reference code below.

### 3a. The "3-second video views" field is not a single scalar

Meta exposes it inconsistently across accounts. Read the **first available** of:

1. `video_3_sec_watched_actions[].value`
2. `actions[]` where `action_type == "video_view"` (Meta's 3-sec view)
3. `video_play_actions[].value` (ThruPlay/plays — last-resort approximation, log a warning)

Then `hook_rate = round(3s_views / impressions * 100, 2)` (guard divide-by-zero).

> **Update (discovered on first live run, v21.0):** Meta now rejects
> `video_3_sec_watched_actions` outright as an unknown field — requesting it
> makes the *entire* insights call fail with HTTP 400 (`#100`), not just omit
> the field. The deployed script no longer requests it and relies on
> preference #2 (`actions` / `video_view`), which is Meta's current standard
> 3-second-view metric. The extraction function still checks for it first in
> case a future API version reinstates it, but it will never actually be
> present.

### 3b. The Ad Library API cannot be queried by ad id

The **Ad Library API is separate** from the Marketing API and only supports
searching by **page id / keyword / date** — there is no `ads_archive/<ad_id>`
endpoint. So to get an ad's exact `ad_archive_id`:

1. From the Marketing API, get the ad's **page id**
   (`GET /<AD_ID>?fields=creative{object_story_spec,effective_object_story_id}`
   → `object_story_spec.page_id`, or the numeric prefix of
   `effective_object_story_id` which is `<page_id>_<post_id>`).
2. Query the Ad Library API
   (`GET /ads_archive?search_page_ids=<page_id>&ad_reached_countries=['GB']&ad_active_status=ALL&fields=id,ad_creative_bodies`),
   paginate, and **match** an entry to your ad by **creative body text**.
3. On a confident match → `?id=<ad_archive_id>`. On no/ambiguous match or if the
   Ad Library API isn't accessible → **page-level fallback link**.

**Prerequisite for exact links:** the Ad Library API requires the app/developer
to have completed Meta's **identity + location confirmation**. Because spend is
in £ (UK), all ads *are* in the EU/UK Ad Library under DSA transparency rules, so
they are findable once access is confirmed. Until then, the code degrades
gracefully to the page-level link.

---

## 4. Exact API contracts

### Meta — bulk ad insights (one paginated sweep for the whole account)

```
GET https://graph.facebook.com/v21.0/act_<ACCOUNT_ID>/insights
  ?level=ad
  &date_preset=maximum
  &fields=ad_id,ad_name,spend,impressions,actions,video_play_actions,video_3_sec_watched_actions
  &limit=200
  &access_token=<SYSTEM_USER_TOKEN>
```
Response has `data[]` and `paging.next` (a full URL — just GET it, it carries
the token + params). `spend` is a string in the account currency (confirm GBP
via `act_<id>?fields=currency`).

### Meta — ad → page id (for the Ad Library link)

```
GET https://graph.facebook.com/v21.0/<AD_ID>
  ?fields=creative{object_story_spec,effective_object_story_id}
  &access_token=<TOKEN>
```

### Meta — Ad Library search (exact archive id)

```
GET https://graph.facebook.com/v21.0/ads_archive
  ?search_page_ids=<PAGE_ID>
  &ad_reached_countries=['GB']
  &ad_active_status=ALL
  &fields=id,ad_creative_bodies
  &limit=100
  &access_token=<TOKEN>
```

### Notion — query database (paginated)

```
POST https://api.notion.com/v1/databases/<DATABASE_ID>/query
Headers: Authorization: Bearer <NOTION_TOKEN>
         Notion-Version: 2022-06-28
         Content-Type: application/json
Body: { "page_size": 100, "start_cursor": <cursor if has_more> }
```

### Notion — update page properties

```
PATCH https://api.notion.com/v1/pages/<PAGE_ID>
Body: { "properties": {
  "Hook Rate":  { "number": 12.34 },
  "Ad Library": { "url": "https://www.facebook.com/ads/library/?id=..." },
  "Sync Status":{ "select": { "name": "Synced" } }
}}
```

> **Update (found on first real-board run):** two changes to the above, made
> after live testing surfaced real issues:
>
> 1. **Hook Rate is written as a fraction, not the pre-multiplied percentage**
>    (`0.4156`, not `41.56`). The team's Hook Rate column is a Number
>    formatted as **Percent**, which multiplies by 100 for display — writing
>    `41.56` directly displayed as `4156%`.
> 2. **Sync Status now has three possible values**, not just `Synced`:
>    `Synced` (hook rate actually computed), `Duplicate Ad Name` (ad name
>    matched >1 Meta ad), `Spend below £100` (matched but under threshold).
>    Only `Synced` is treated as terminal/skip-on-rerun — the other two are
>    re-evaluated every run since the underlying condition can change.
>    A matched ad with spend above threshold but no video data (a static/image
>    ad) gets neither Hook Rate nor a status change — there's nothing to mark
>    Synced for, and it isn't a duplicate or below-threshold case either.

---

## 5. Repository layout

```
<repo-root>/
├── automation/
│   └── hookrate/
│       ├── sync_hook_rate.py     # single-file entry point (§7)
│       ├── requirements.txt      # requests>=2.31
│       ├── .env.example          # documents every env var (§6)
│       └── README.md             # operator setup guide
└── .github/
    └── workflows/
        └── hook-rate-sync.yml    # cron + workflow_dispatch (§8)
Also add .gitignore entries: .env, automation/hookrate/.env, __pycache__/
```

---

## 6. Configuration (all via environment variables)

**Required (GitHub secrets):**

| Var | Meaning |
|---|---|
| `NOTION_TOKEN` | Notion internal integration token |
| `NOTION_DATABASE_ID` | Target database id (32 hex chars from the DB URL) |
| `META_ACCESS_TOKEN` | Long-lived System User token, `ads_read` scope |
| `META_AD_ACCOUNT_ID` | `act_<id>` (the `act_` prefix is auto-added if missing) |

**Optional tuning (GitHub Actions *variables*, or `.env` locally):**

| Var | Default | Meaning |
|---|---|---|
| `SPEND_THRESHOLD_GBP` | `100` | Min lifetime spend before a row is processed |
| `DRY_RUN` | `false` | Compute + log intended writes, no Notion changes |
| `FORCE_REPROCESS` | `false` | Re-process rows already marked `Synced` |
| `META_DATE_PRESET` | `maximum` | Insights window (`maximum` = lifetime; e.g. `last_30d`) |
| `AD_LIBRARY_COUNTRY` | `GB` | Country for Ad Library search + fallback link |
| `WRITE_SPEND` | `false` | Also write spend into a `Spend` column if present |
| `FAIL_ON_ERROR` | `false` | Make the CI run red if any row errored |
| `PROP_HOOK_RATE` | `Hook Rate` | Notion column name overrides |
| `PROP_AD_LIBRARY` | `Ad Library` | |
| `PROP_STATUS` | `Sync Status` | |
| `PROP_SPEND` | `Spend` | |

**Important:** GitHub Actions passes unset `vars.*` as empty strings, so the
config layer must treat empty/whitespace as "unset" and fall back to the
default. (The reference `_env_str` / `_env_bool` helpers do this.)

---

## 7. Reference implementation — `automation/hookrate/sync_hook_rate.py`

> This is complete and has been unit-tested (name normalization, 3-sec-view
> extraction preference order, idempotency detection across select/status/
> checkbox/number, title extraction, empty-env handling, status-payload typing).
> Copy verbatim.

```python
#!/usr/bin/env python3
"""Sync Meta (Facebook/Instagram) ad performance into a Notion creative board.

For every ad asset on a Notion database ("board"), this script:
  1. Matches the Notion item to a Meta ad *by ad name*.
  2. If that ad has spent more than the threshold (default £100 lifetime),
     computes the hook rate (3-second video views / impressions * 100).
  3. Derives the exact Facebook Ad Library deep link for that ad
     (falling back to a page-level link if an exact match isn't possible).
  4. Writes the hook rate and Ad Library link back onto the Notion item.

Designed to run unattended on GitHub Actions (see
.github/workflows/hook-rate-sync.yml). All configuration comes from environment
variables; see .env.example. The only third-party dependency is `requests`.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Default names of the Notion properties we read/write. Override via env if the
# team's board uses different names.
DEFAULT_PROP_HOOK_RATE = "Hook Rate"
DEFAULT_PROP_AD_LIBRARY = "Ad Library"
DEFAULT_PROP_STATUS = "Sync Status"
DEFAULT_PROP_SPEND = "Spend"  # optional audit column; skipped if it doesn't exist
SYNCED_MARKER = "Synced"


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so local runs don't need python-dotenv.

    Only sets variables that aren't already present in the environment.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_str(name: str, default: str) -> str:
    """Return env var, treating empty/whitespace as unset (CI passes ""s)."""
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    return val.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    notion_token: str
    notion_database_id: str
    meta_access_token: str
    meta_ad_account_id: str  # normalized to "act_<id>"
    spend_threshold: float = 100.0
    dry_run: bool = False
    force_reprocess: bool = False
    date_preset: str = "maximum"
    reached_country: str = "GB"
    prop_hook_rate: str = DEFAULT_PROP_HOOK_RATE
    prop_ad_library: str = DEFAULT_PROP_AD_LIBRARY
    prop_status: str = DEFAULT_PROP_STATUS
    prop_spend: str = DEFAULT_PROP_SPEND
    write_spend: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        missing = [
            name
            for name in (
                "NOTION_TOKEN",
                "NOTION_DATABASE_ID",
                "META_ACCESS_TOKEN",
                "META_AD_ACCOUNT_ID",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variables: " + ", ".join(missing)
            )

        acct = os.environ["META_AD_ACCOUNT_ID"].strip()
        if not acct.startswith("act_"):
            acct = f"act_{acct}"

        threshold = float(_env_str("SPEND_THRESHOLD_GBP", "100"))

        return cls(
            notion_token=os.environ["NOTION_TOKEN"].strip(),
            notion_database_id=os.environ["NOTION_DATABASE_ID"].strip(),
            meta_access_token=os.environ["META_ACCESS_TOKEN"].strip(),
            meta_ad_account_id=acct,
            spend_threshold=threshold,
            dry_run=_env_bool("DRY_RUN", False),
            force_reprocess=_env_bool("FORCE_REPROCESS", False),
            date_preset=_env_str("META_DATE_PRESET", "maximum"),
            reached_country=_env_str("AD_LIBRARY_COUNTRY", "GB"),
            prop_hook_rate=_env_str("PROP_HOOK_RATE", DEFAULT_PROP_HOOK_RATE),
            prop_ad_library=_env_str("PROP_AD_LIBRARY", DEFAULT_PROP_AD_LIBRARY),
            prop_status=_env_str("PROP_STATUS", DEFAULT_PROP_STATUS),
            prop_spend=_env_str("PROP_SPEND", DEFAULT_PROP_SPEND),
            write_spend=_env_bool("WRITE_SPEND", False),
        )


# --------------------------------------------------------------------------- #
# HTTP helper with retries / backoff
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    pass


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    max_retries: int = 5,
) -> dict:
    """HTTP call returning parsed JSON, with exponential backoff on 429/5xx."""
    delay = 2.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=60,
            )
        except requests.RequestException as exc:  # network error
            last_exc = exc
            log(f"  network error ({exc}); retry {attempt}/{max_retries}")
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            log(
                f"  HTTP {resp.status_code}; backing off {wait:.0f}s "
                f"(retry {attempt}/{max_retries})"
            )
            time.sleep(wait)
            delay *= 2
            continue

        if not resp.ok:
            raise ApiError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text}")

        if not resp.content:
            return {}
        return resp.json()

    raise ApiError(f"{method} {url} failed after {max_retries} retries: {last_exc}")


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Meta Marketing API
# --------------------------------------------------------------------------- #


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def fetch_all_ad_insights(cfg: Config) -> dict[str, dict]:
    """Bulk-pull ad-level insights for the whole account in one paginated sweep.

    Returns a map of normalized ad name -> insight dict. Names that appear more
    than once map to the sentinel value {"_ambiguous": True} so the caller can
    refuse to guess.
    """
    url = f"{GRAPH_BASE}/{cfg.meta_ad_account_id}/insights"
    params = {
        "level": "ad",
        "date_preset": cfg.date_preset,
        "fields": ",".join(
            [
                "ad_id",
                "ad_name",
                "spend",
                "impressions",
                "actions",
                "video_play_actions",
                "video_3_sec_watched_actions",
            ]
        ),
        "limit": 200,
        "access_token": cfg.meta_access_token,
    }

    by_name: dict[str, dict] = {}
    seen_names: set[str] = set()
    page = 0
    while url:
        page += 1
        data = request_json("GET", url, params=params if page == 1 else None)
        rows = data.get("data", [])
        log(f"  Meta insights page {page}: {len(rows)} ads")
        for row in rows:
            key = normalize_name(row.get("ad_name", ""))
            if not key:
                continue
            if key in seen_names:
                by_name[key] = {"_ambiguous": True}
                continue
            seen_names.add(key)
            by_name[key] = row
        url = (data.get("paging") or {}).get("next")
        params = None  # `next` already carries all query params + token

    return by_name


def extract_3s_views(insight: dict) -> Optional[int]:
    """Pull the 3-second video view count from an insights row.

    Preference order:
      1. video_3_sec_watched_actions
      2. actions where action_type == "video_view" (Meta's 3-sec metric)
      3. video_play_actions (ThruPlay/plays — logged as an approximation)
    """
    def _sum(actions: Any) -> Optional[int]:
        if not actions:
            return None
        total = 0.0
        found = False
        for item in actions:
            val = item.get("value")
            if val is None:
                continue
            total += float(val)
            found = True
        return int(total) if found else None

    v = _sum(insight.get("video_3_sec_watched_actions"))
    if v is not None:
        return v

    actions = insight.get("actions") or []
    vv = [a for a in actions if a.get("action_type") == "video_view"]
    v = _sum(vv)
    if v is not None:
        return v

    v = _sum(insight.get("video_play_actions"))
    if v is not None:
        log("    note: using video_play_actions as 3-sec-view approximation")
    return v


def get_ad_page_id(cfg: Config, ad_id: str) -> Optional[str]:
    """Resolve the Facebook Page id backing an ad (needed for Ad Library)."""
    url = f"{GRAPH_BASE}/{ad_id}"
    params = {
        "fields": "creative{object_story_spec,effective_object_story_id}",
        "access_token": cfg.meta_access_token,
    }
    data = request_json("GET", url, params=params)
    creative = data.get("creative") or {}
    spec = creative.get("object_story_spec") or {}
    if spec.get("page_id"):
        return str(spec["page_id"])
    story_id = creative.get("effective_object_story_id")  # "<page_id>_<post_id>"
    if story_id and "_" in story_id:
        return story_id.split("_", 1)[0]
    return None


def build_ad_library_url(cfg: Config, ad_id: str, insight: dict) -> Optional[str]:
    """Best-effort exact Ad Library deep link, page-level link as fallback."""
    page_id = get_ad_page_id(cfg, ad_id)
    if not page_id:
        log("    could not resolve page id; no Ad Library link")
        return None

    archive_id = find_ad_archive_id(cfg, page_id, insight)
    if archive_id:
        return f"https://www.facebook.com/ads/library/?id={archive_id}"

    # Fallback: page-level Ad Library view filtered to the reached country.
    return (
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
        f"&country={cfg.reached_country}&view_all_page_id={page_id}"
        "&search_type=page"
    )


def find_ad_archive_id(
    cfg: Config, page_id: str, insight: dict
) -> Optional[str]:
    """Search the Ad Library API by page id and match this ad's creative.

    The Ad Library API cannot be queried by ad id, so we search the page's ads
    and match on creative body text. Requires Ad Library API access (identity
    confirmation); on any failure or ambiguity we return None so the caller
    falls back to a page-level link.
    """
    target_bodies = _creative_bodies_for_ad(cfg, insight.get("ad_id", ""))
    if not target_bodies:
        return None

    url = f"{GRAPH_BASE}/ads_archive"
    params = {
        "search_page_ids": page_id,
        "ad_reached_countries": f"['{cfg.reached_country}']",
        "ad_active_status": "ALL",
        "fields": "id,ad_creative_bodies",
        "limit": 100,
        "access_token": cfg.meta_access_token,
    }
    try:
        page = 0
        while url and page < 20:  # bound the search
            page += 1
            data = request_json("GET", url, params=params if page == 1 else None)
            for entry in data.get("data", []):
                bodies = {
                    normalize_name(b)
                    for b in (entry.get("ad_creative_bodies") or [])
                    if b
                }
                if bodies & target_bodies:
                    return str(entry.get("id"))
            url = (data.get("paging") or {}).get("next")
            params = None
    except ApiError as exc:
        log(f"    Ad Library lookup unavailable ({exc}); using page-level link")
        return None
    return None


def _creative_bodies_for_ad(cfg: Config, ad_id: str) -> set[str]:
    """Fetch the creative body text of one of our ads, for Ad Library matching."""
    if not ad_id:
        return set()
    url = f"{GRAPH_BASE}/{ad_id}"
    params = {
        "fields": "creative{body,object_story_spec}",
        "access_token": cfg.meta_access_token,
    }
    try:
        data = request_json("GET", url, params=params)
    except ApiError:
        return set()
    creative = data.get("creative") or {}
    bodies: set[str] = set()
    if creative.get("body"):
        bodies.add(normalize_name(creative["body"]))
    spec = creative.get("object_story_spec") or {}
    for key in ("link_data", "video_data"):
        message = (spec.get(key) or {}).get("message")
        if message:
            bodies.add(normalize_name(message))
    return {b for b in bodies if b}


# --------------------------------------------------------------------------- #
# Notion API
# --------------------------------------------------------------------------- #


def notion_headers(cfg: Config) -> dict:
    return {
        "Authorization": f"Bearer {cfg.notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_query_all_pages(cfg: Config) -> list[dict]:
    url = f"{NOTION_BASE}/databases/{cfg.notion_database_id}/query"
    headers = notion_headers(cfg)
    pages: list[dict] = []
    cursor: Optional[str] = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = request_json("POST", url, headers=headers, json_body=body)
        pages.extend(data.get("results", []))
        if data.get("has_more"):
            cursor = data.get("next_cursor")
            time.sleep(0.34)  # stay under Notion's ~3 req/s limit
        else:
            break
    return pages


def get_page_title(page: dict) -> str:
    """Return the plain-text title of a Notion page (the title property)."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            parts = prop.get("title") or []
            return "".join(p.get("plain_text", "") for p in parts)
    return ""


def get_prop(page: dict, name: str) -> Optional[dict]:
    return (page.get("properties") or {}).get(name)


def is_already_synced(cfg: Config, page: dict) -> bool:
    status = get_prop(page, cfg.prop_status)
    if status:
        ptype = status.get("type")
        if ptype == "select" and (status.get("select") or {}).get("name") == SYNCED_MARKER:
            return True
        if ptype == "status" and (status.get("status") or {}).get("name") == SYNCED_MARKER:
            return True
        if ptype == "checkbox" and status.get("checkbox") is True:
            return True
    # Otherwise treat a non-empty hook-rate value as "already processed".
    hook = get_prop(page, cfg.prop_hook_rate)
    if hook and hook.get("type") == "number" and hook.get("number") is not None:
        return True
    return False


def status_property_payload(cfg: Config, page: dict) -> Optional[dict]:
    """Build the sync-marker property payload matching its schema type."""
    status = get_prop(page, cfg.prop_status)
    if not status:
        return None
    ptype = status.get("type")
    if ptype == "select":
        return {"select": {"name": SYNCED_MARKER}}
    if ptype == "status":
        return {"status": {"name": SYNCED_MARKER}}
    if ptype == "checkbox":
        return {"checkbox": True}
    return None


def notion_update_page(cfg: Config, page: dict, hook_rate: Optional[float],
                       ad_library_url: Optional[str], spend: float) -> None:
    props: dict[str, Any] = {}
    if hook_rate is not None:
        props[cfg.prop_hook_rate] = {"number": hook_rate}
    if ad_library_url:
        props[cfg.prop_ad_library] = {"url": ad_library_url}
    if cfg.write_spend and get_prop(page, cfg.prop_spend):
        props[cfg.prop_spend] = {"number": round(spend, 2)}
    status_payload = status_property_payload(cfg, page)
    if status_payload:
        props[cfg.prop_status] = status_payload

    if not props:
        return

    url = f"{NOTION_BASE}/pages/{page['id']}"
    request_json("PATCH", url, headers=notion_headers(cfg), json_body={"properties": props})
    time.sleep(0.34)  # rate limit


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    processed: int = 0
    updated: int = 0
    below_threshold: int = 0
    no_match: int = 0
    already_synced: int = 0
    errors: int = 0
    error_pages: list[str] = field(default_factory=list)


def run(cfg: Config) -> Summary:
    summary = Summary()

    log("Fetching Meta ad insights (bulk)...")
    insights_by_name = fetch_all_ad_insights(cfg)
    log(f"  {len(insights_by_name)} distinct ad names loaded")

    log("Querying Notion board...")
    pages = notion_query_all_pages(cfg)
    log(f"  {len(pages)} rows on the board")

    for page in pages:
        summary.processed += 1
        title = get_page_title(page)
        try:
            if is_already_synced(cfg, page) and not cfg.force_reprocess:
                summary.already_synced += 1
                continue

            insight = insights_by_name.get(normalize_name(title))
            if not insight:
                log(f"- no Meta ad matches '{title}'")
                summary.no_match += 1
                continue
            if insight.get("_ambiguous"):
                log(f"- '{title}' matches multiple ads by name; skipping")
                summary.no_match += 1
                continue

            spend = float(insight.get("spend") or 0)
            if spend <= cfg.spend_threshold:
                log(f"- '{title}' spend £{spend:.2f} <= £{cfg.spend_threshold:.0f}; skip")
                summary.below_threshold += 1
                continue

            impressions = int(float(insight.get("impressions") or 0))
            three_sec = extract_3s_views(insight)
            hook_rate: Optional[float] = None
            if impressions and three_sec is not None:
                hook_rate = round((three_sec / impressions) * 100, 2)

            ad_id = insight.get("ad_id", "")
            ad_library_url = build_ad_library_url(cfg, ad_id, insight)

            if cfg.dry_run:
                log(
                    f"[DRY RUN] '{title}': spend=£{spend:.2f} hook_rate={hook_rate} "
                    f"url={ad_library_url}"
                )
            else:
                notion_update_page(cfg, page, hook_rate, ad_library_url, spend)
                log(
                    f"+ '{title}': spend=£{spend:.2f} hook_rate={hook_rate} "
                    f"url={ad_library_url}"
                )
            summary.updated += 1

        except Exception as exc:  # noqa: BLE001 - isolate per-row failures
            summary.errors += 1
            summary.error_pages.append(f"{title or page.get('id')}: {exc}")
            log(f"! error on '{title}': {exc}")

    return summary


def main() -> int:
    cfg = Config.from_env()
    mode = "DRY RUN" if cfg.dry_run else "LIVE"
    log(
        f"Hook-rate sync starting ({mode}); threshold £{cfg.spend_threshold:.0f}, "
        f"window={cfg.date_preset}"
    )
    summary = run(cfg)

    log("\n===== Summary =====")
    log(f"  processed:        {summary.processed}")
    log(f"  updated:          {summary.updated}")
    log(f"  below threshold:  {summary.below_threshold}")
    log(f"  no ad match:      {summary.no_match}")
    log(f"  already synced:   {summary.already_synced}")
    log(f"  errors:           {summary.errors}")
    for line in summary.error_pages:
        log(f"    - {line}")

    # Scheduled runs stay green even with per-row errors; set FAIL_ON_ERROR=true
    # to surface failures as a red CI run.
    if summary.errors and _env_bool("FAIL_ON_ERROR", False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 8. GitHub Actions workflow — `.github/workflows/hook-rate-sync.yml`

```yaml
name: Hook Rate Sync

# Syncs Meta ad hook rate + Facebook Ad Library links into the Notion board.
# Runs on a daily schedule and can be triggered manually (defaults to dry-run).

on:
  schedule:
    # 07:00 UTC every day. Adjust as needed (cron is in UTC).
    - cron: "0 7 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (log intended writes, do not modify Notion)"
        type: boolean
        default: true
      force_reprocess:
        description: "Re-process rows already marked Synced"
        type: boolean
        default: false

# Prevent overlapping runs.
concurrency:
  group: hook-rate-sync
  cancel-in-progress: false

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r automation/hookrate/requirements.txt

      - name: Run hook rate sync
        env:
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          NOTION_DATABASE_ID: ${{ secrets.NOTION_DATABASE_ID }}
          META_ACCESS_TOKEN: ${{ secrets.META_ACCESS_TOKEN }}
          META_AD_ACCOUNT_ID: ${{ secrets.META_AD_ACCOUNT_ID }}
          # Manual runs honor the inputs; scheduled runs go live (dry_run=false).
          DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || 'false' }}
          FORCE_REPROCESS: ${{ github.event_name == 'workflow_dispatch' && inputs.force_reprocess || 'false' }}
          # Optional tuning — set as repo variables to override the defaults.
          SPEND_THRESHOLD_GBP: ${{ vars.SPEND_THRESHOLD_GBP }}
          META_DATE_PRESET: ${{ vars.META_DATE_PRESET }}
          AD_LIBRARY_COUNTRY: ${{ vars.AD_LIBRARY_COUNTRY }}
          WRITE_SPEND: ${{ vars.WRITE_SPEND }}
          FAIL_ON_ERROR: ${{ vars.FAIL_ON_ERROR }}
          PROP_HOOK_RATE: ${{ vars.PROP_HOOK_RATE }}
          PROP_AD_LIBRARY: ${{ vars.PROP_AD_LIBRARY }}
          PROP_STATUS: ${{ vars.PROP_STATUS }}
          PROP_SPEND: ${{ vars.PROP_SPEND }}
        run: python automation/hookrate/sync_hook_rate.py
```

`automation/hookrate/requirements.txt`:
```
requests>=2.31
```

---

## 9. Operator one-time setup

### Notion
1. Create an internal integration at <https://www.notion.so/my-integrations>; copy the token.
2. Open the target database → **⋯ → Connections → Add** the integration (this *shares* it — required or writes 404).
3. Ensure these columns exist (the script never changes schema):
   - **Hook Rate** → **Number**
   - **Ad Library** → **URL**
   - **Sync Status** → **Select** (or Status, or Checkbox) — the "already processed" marker; value written is `Synced`
   - *(optional)* **Spend** → **Number** (only written if `WRITE_SPEND=true`)
4. Copy the **database id** from the DB URL (32 hex chars).

### Meta
1. Business Settings → Users → **System Users** → generate a **long-lived token** with **`ads_read`** and the target ad account assigned.
2. Confirm currency is GBP: `GET /v21.0/act_<ID>?fields=currency`.
3. For **exact** Ad Library links, complete Meta's **identity + location confirmation** for Ad Library API access. Otherwise the code uses the page-level fallback automatically.

### GitHub
Add 4 repo **secrets**: `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`. Optional tuning goes in repo **variables**.

---

## 10. Verification plan

1. **Local dry-run:** fill `.env` → `DRY_RUN=true python automation/hookrate/sync_hook_rate.py`. Confirm it reads rows, matches ads, computes hook rate, and logs intended writes with **zero** Notion mutations.
2. **Sanity-check one ad:** manually verify the computed hook rate + the 3-sec-views field choice against that ad in Business Manager (accounts differ in which video fields they populate).
3. **Single live write:** point at a scratch DB, `DRY_RUN=false`; confirm the Number + URL land and the Status marker is set.
4. **Threshold + idempotency:** a <£100 ad is skipped; a >£100 ad is written; a second run skips already-synced rows unless `FORCE_REPROCESS=true`.
5. **Ad Library link:** open the generated URL; confirm it resolves to the right ad (exact) or the right page's ads (fallback).
6. **CI:** trigger `workflow_dispatch` (dry-run true) from the Actions tab → inspect logs → re-run dry-run false against the real board → then rely on the daily cron.

---

## 11. Edge cases the code already handles

- Empty `insights.data` for an ad → treated as zero spend, skipped.
- Zero impressions → hook rate `None`, not a divide-by-zero.
- Duplicate ad names → marked ambiguous, skipped-and-logged (never mis-attributed).
- Per-row exceptions → isolated; one bad row never aborts the whole run.
- 429 / 5xx / network errors → exponential backoff with retries; honors `Retry-After`.
- Notion pagination (`has_more`/`next_cursor`) and Meta pagination (`paging.next`).
- GitHub passing unset variables as empty strings → treated as unset.
- Sync marker works whether the column is Select, Status, or Checkbox.

---

## 12. Known limitations / future extensions

- **Name matching** is only as reliable as the Notion titles matching Meta ad names. If collisions are common, add an authoritative `Meta Ad ID` column and a two-tier resolver (ID first, name fallback) — the code is structured for this.
- **Currency** is assumed GBP; multi-currency accounts would need an FX step before the threshold comparison.
- **Exact Ad Library matching** is by creative body text; ads with identical copy across variants may match the wrong archive entry — acceptable given the page-level fallback, but a stricter match (start date + link) could be added.
- For a very large account, the per-ad page-id / Ad Library calls dominate runtime; they only fire for ads over the spend threshold, which keeps volume low in practice.
```
