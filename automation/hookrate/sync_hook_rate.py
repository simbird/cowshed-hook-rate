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
DEFAULT_PROP_AD_LIBRARY = "Ad Link"
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
    test_ad_name: Optional[str] = None

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
            test_ad_name=os.environ.get("TEST_AD_NAME", "").strip() or None,
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
      1. video_3_sec_watched_actions (kept for accounts/API versions where it
         still appears; current Graph API versions reject it as an unknown
         field if requested, so fetch_all_ad_insights no longer requests it)
      2. actions where action_type == "video_view" (Meta's current 3-sec metric)
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

    if cfg.test_ad_name:
        target = normalize_name(cfg.test_ad_name)
        pages = [p for p in pages if normalize_name(get_page_title(p)) == target]
        log(f"  TEST_AD_NAME set; filtered to {len(pages)} matching row(s)")

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
