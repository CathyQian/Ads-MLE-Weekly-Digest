"""industry_feeds.py — Fetch and parse RSS/Atom and HTML industry blog feeds.

Designed to be called from weekly_runner.py after the arXiv pipeline.
Stores items in docs/industry.json (separate from docs/papers.json).
Tracks per-feed ingestion cursors in docs/industry_feed_state.json so that
each run only ingests items published after the last successful run.

Cursor update rule (documented to avoid ambiguity):
  After a fully successful fetch of a feed's XML, the cursor is set to the
  MAX(published datetime) across ALL entries in that feed's XML — not just the
  newly included items.  This means on the next run every item with
  published <= that max will be skipped, even if it was not included this run
  (e.g. it hit the max_items cap).  The effect is that only items published
  after the last run's newest entry will be ingested on future runs.
  If no entry has a parseable published date, the cursor is set to the
  run-end UTC time to prevent re-ingesting on next run.

HTML scraper mode:
  Feeds with scrape_type: "anthropic" use a listing-page HTML scraper instead
  of RSS/Atom. The scraper extracts article slugs, titles, and dates from
  ArticleList, FeaturedGrid, and PublicationList layout blocks, then fetches
  each individual article for og:description as abstract.
"""

import html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple
import requests
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ATOM_NS           = "{http://www.w3.org/2005/Atom}"
DOCS_DIR          = "docs"
INDUSTRY_JSON     = os.path.join(DOCS_DIR, "industry.json")
INDUSTRY_STATE    = os.path.join(DOCS_DIR, "industry_feed_state.json")
MAX_RETRIES       = 3

_TAG_RE = re.compile(r"<[^>]+>")

# Generic Anthropic site description — not a useful article abstract.
_ANTHROPIC_GENERIC_DESC = "Anthropic is an AI safety"

_MONTH_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}",
    re.I,
)


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities, returning collapsed plain text."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_feed_date(date_str: str, fallback: str) -> Tuple[str, Optional[datetime]]:
    """Parse an RSS/Atom date string to (YYYY-MM-DD, timezone-aware datetime).

    Returns (fallback, None) when the string is empty or cannot be parsed.
    The datetime is needed for cursor comparisons; the string is stored in JSON.
    Supported formats:
      - ISO 8601 / Atom: "2026-04-12T10:00:00Z" or "+00:00" offset
      - RFC 2822 (RSS pubDate): "Sat, 12 Apr 2026 10:00:00 +0000"
      - Human-readable listing: "Apr 9, 2026" or "April 9, 2026"
    """
    if not date_str:
        return fallback, None

    # Atom / ISO 8601: "2026-04-12T10:00:00Z" or "+00:00" offset
    normalised = date_str.strip()
    if normalised.endswith("Z"):
        normalised = normalised[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # RFC 2822 (RSS pubDate): "Sat, 12 Apr 2026 10:00:00 +0000"
    try:
        dt = parsedate_to_datetime(date_str).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Human-readable listing dates: "Apr 9, 2026" or "April 9, 2026"
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d"), dt
        except ValueError:
            pass

    return fallback, None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(data, path: str) -> None:
    """Write JSON atomically via tempfile + os.replace()."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile(
        "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def load_feed_state(path: str = INDUSTRY_STATE) -> dict:
    """Load per-feed cursor state: {feed_url: last_seen_published_iso_utc}."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_feed_state(state: dict, path: str = INDUSTRY_STATE) -> None:
    """Atomically persist feed cursor state."""
    _atomic_write_json(state, path)


def load_industry_items(path: str = INDUSTRY_JSON) -> list:
    """Load existing industry items. Returns [] if file is missing."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_industry_items(items: list, path: str = INDUSTRY_JSON) -> None:
    """Atomically write the full industry item list."""
    _atomic_write_json(items, path)


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

def _fetch_with_retry(url: str, feed_name: str) -> Optional[requests.Response]:
    """GET url with exponential-backoff retries. Returns None on total failure."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as exc:
            print(f"[WARN]  Request error '{feed_name}' (attempt {attempt + 1}): {exc}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"[WARN]  Rate limited on '{feed_name}', retrying in {wait}s")
            time.sleep(wait)
        else:
            print(f"[WARN]  HTTP {resp.status_code} for '{feed_name}' (attempt {attempt + 1})")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

    print(f"[ERROR] Failed to fetch '{feed_name}' after {MAX_RETRIES} attempts — skipping")
    return None


def _parse_entries(root: ET.Element) -> Tuple[list, bool]:
    """Return (entries, is_atom) from a parsed feed root element."""
    tag = root.tag.lower()
    is_atom = "feed" in tag
    if is_atom:
        return root.findall(f"{ATOM_NS}entry"), True
    channel = root.find("channel") or root
    return channel.findall("item"), False


def _entry_fields(entry: ET.Element, is_atom: bool) -> Tuple[str, str, str, str]:
    """Extract (title, url, raw_description, date_str) from a feed entry."""
    if is_atom:
        title_el = entry.find(f"{ATOM_NS}title")
        title = (title_el.text or "").strip() if title_el is not None else ""

        link_el = entry.find(f"{ATOM_NS}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{ATOM_NS}link")
        url = (link_el.attrib.get("href") or "").strip() if link_el is not None else ""

        body_el = entry.find(f"{ATOM_NS}summary")
        if body_el is None:
            body_el = entry.find(f"{ATOM_NS}content")
        raw = (body_el.text or "") if body_el is not None else ""

        pub_el = entry.find(f"{ATOM_NS}published") or entry.find(f"{ATOM_NS}updated")
        date_str = (pub_el.text or "").strip() if pub_el is not None else ""
    else:
        title_el = entry.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""

        link_el = entry.find("link")
        url = (link_el.text or "").strip() if link_el is not None else ""

        desc_el = entry.find("description")
        raw = (desc_el.text or "") if desc_el is not None else ""

        pub_el = entry.find("pubDate")
        date_str = (pub_el.text or "").strip() if pub_el is not None else ""

    return title, url, raw, date_str


# ---------------------------------------------------------------------------
# HTML blog scraper (for sites without RSS, e.g. Anthropic)
# ---------------------------------------------------------------------------

def _article_abstract(article_url: str, feed_name: str) -> Tuple[str, str]:
    """Fetch an individual article page; return (abstract, iso_date_or_empty)."""
    resp = _fetch_with_retry(article_url, feed_name)
    if resp is None:
        return "", ""
    text = resp.text

    # Abstract: og:description (skip generic Anthropic boilerplate)
    og = re.search(r'property="og:description"\s+content="([^"]+)"', text)
    if not og:
        og = re.search(r'content="([^"]+)"\s+property="og:description"', text)
    abstract = ""
    if og:
        raw = html.unescape(og.group(1))
        if not raw.startswith(_ANTHROPIC_GENERIC_DESC):
            abstract = raw.strip()

    # Date: look for ISO 8601 date near the top of the page
    iso = re.search(r'"(\d{4}-\d{2}-\d{2})"', text[:8000])
    iso_date = iso.group(1) if iso else ""

    return abstract, iso_date


def _scrape_anthropic_blog(
    feed_cfg: dict,
    fetched_date: str,
    cursor_dt: Optional[datetime],
    existing_urls: set,
    max_items: int,
) -> Tuple[list, Optional[datetime]]:
    """Scrape an Anthropic-style HTML blog listing (engineering or research).

    Handles three page layouts:
      - ArticleList  (engineering): <article class="...ArticleList...">
      - FeaturedGrid (research):    <a href="/path/..."><h2/h3/h4>…</a>
      - PublicationList (research): <a href="/path/..."><span class="...title...">…</a>

    Returns (new_items, max_pub_dt_seen_across_all_entries).
    """
    feed_url  = feed_cfg["url"]
    feed_name = feed_cfg.get("name") or feed_url
    topic     = feed_cfg.get("topic") or "industry-blog"
    base_url  = feed_cfg.get("base_url", "https://www.anthropic.com")
    path_pfx  = feed_cfg.get("path_prefix", "")

    resp = _fetch_with_retry(feed_url, feed_name)
    if resp is None:
        return [], None

    # --- Extract article blocks ---

    # Try ArticleList <article> blocks (engineering-style layout)
    article_blocks = re.findall(
        r'<article[^>]*class="[^"]*ArticleList[^"]*"[^>]*>(.*?)</article>',
        resp.text,
        re.S,
    )

    # Fall back: anchor-based layouts (research-style pages)
    # Handles FeaturedGrid (h2/h3/h4 inside anchor) and
    # PublicationList (span.title inside anchor, no h-tags).
    if not article_blocks:
        raw_anchors = re.findall(
            r'<a[^>]+href="(' + re.escape(path_pfx) + r'[^"?#]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.S,
        )
        article_blocks = []
        for slug, content in raw_anchors:
            has_heading   = bool(re.search(r'<h[234][^>]*>', content, re.S))
            has_title_span = bool(re.search(r'<span[^>]+class="[^"]*title[^"]*"', content, re.S))
            if has_heading or has_title_span:
                article_blocks.append(f'<a href="{slug}">{content}</a>')

    if not article_blocks:
        print(f"[WARN]  No article blocks found for '{feed_name}'")
        return [], None

    max_pub_dt: Optional[datetime] = None
    new_items: list = []
    included = 0

    for block in article_blocks:
        # Slug / URL
        slug_m = re.search(r'href="(' + re.escape(path_pfx) + r'[^"?#]+)"', block)
        if not slug_m:
            continue
        article_url = base_url + slug_m.group(1)

        # Title: prefer <h2>/<h3>/<h4>, fall back to <span class="...title...">
        h_tag = re.search(r"<h[234][^>]*>(.*?)</h[234]>", block, re.S)
        if h_tag:
            title = strip_html(h_tag.group(1)).strip()
        else:
            sp = re.search(r'<span[^>]+class="[^"]*title[^"]*"[^>]*>(.*?)</span>', block, re.S)
            title = strip_html(sp.group(1)).strip() if sp else ""
        if not title:
            continue

        # Date from listing page (tries "Mar 25, 2026" and "Mar 5, 2026" formats)
        date_m = _MONTH_RE.search(block)
        date_str = date_m.group(0) if date_m else ""
        date, pub_dt = parse_feed_date(date_str, fetched_date)

        # Advance global max (cursor uses all entries, not just included ones)
        if pub_dt is not None:
            if max_pub_dt is None or pub_dt > max_pub_dt:
                max_pub_dt = pub_dt

        # Cursor filter
        if cursor_dt is not None and pub_dt is not None and pub_dt <= cursor_dt:
            continue

        # URL dedup
        if article_url in existing_urls:
            print(f"[INFO]   SKIP (dup URL): {title[:70]}")
            continue

        # Cap
        if included >= max_items:
            continue

        # Inline description from listing page (PublicationList cards may have <p>)
        inline_p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        inline_abstract = strip_html(inline_p.group(1)).strip() if inline_p else ""

        # Fetch individual article for abstract and date when missing from listing
        abstract, iso_date = _article_abstract(article_url, feed_name)
        abstract = abstract or inline_abstract

        if not date_str and iso_date:
            date, pub_dt = parse_feed_date(iso_date, fetched_date)
            if pub_dt is not None:
                if max_pub_dt is None or pub_dt > max_pub_dt:
                    max_pub_dt = pub_dt
                # Re-check cursor now that we have a real date
                if cursor_dt is not None and pub_dt <= cursor_dt:
                    continue

        new_items.append({
            "title":        title,
            "abstract":     abstract,
            "url":          article_url,
            "topic":        topic,
            "date":         date,
            "fetched_date": fetched_date,
            "feed_name":    feed_name,
            "source":       "industry",
        })
        existing_urls.add(article_url)
        included += 1
        print(f"[INFO]   NEW [{feed_name}]: {title[:70]}")
        time.sleep(0.5)  # be polite between article fetches

    print(f"[INFO] [{feed_name}] {included} new item(s) ingested")
    return new_items, max_pub_dt


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_industry_feeds(
    feeds_config: list,
    fetched_date: str,
    existing_state: dict,
    existing_urls: set,
) -> Tuple[list, dict]:
    """Fetch each configured RSS/Atom or HTML feed and return (new_items, updated_state).

    Args:
        feeds_config:   list of feed dicts from config.yaml (industry_feeds).
        fetched_date:   today as YYYY-MM-DD string (used as fallback date).
        existing_state: loaded from industry_feed_state.json.
        existing_urls:  set of URLs already in industry.json (mutated in place).

    Returns:
        new_items:     list of item dicts ready to append to industry.json.
        updated_state: copy of existing_state with cursors updated for each
                       successfully fetched feed.
    """
    new_state = dict(existing_state)
    all_new: list = []

    for feed_cfg in feeds_config:
        feed_url  = (feed_cfg.get("url") or "").strip()
        feed_name = feed_cfg.get("name") or feed_url
        max_items = int(feed_cfg.get("max_items") or 10)

        if not feed_url:
            print(f"[WARN]  Skipping feed with no URL: {feed_name!r}")
            continue

        # Resolve cursor for this feed
        cursor_dt: Optional[datetime] = None
        cursor_str = existing_state.get(feed_url)
        if cursor_str:
            try:
                cursor_dt = datetime.fromisoformat(cursor_str)
                if cursor_dt.tzinfo is None:
                    cursor_dt = cursor_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                cursor_dt = None

        print(f"[INFO] Fetching industry feed: {feed_name}")

        # --- Dispatch: HTML scraper vs RSS/Atom ---
        if feed_cfg.get("scrape_type") == "anthropic":
            feed_new, max_pub_dt = _scrape_anthropic_blog(
                feed_cfg, fetched_date, cursor_dt, existing_urls, max_items
            )
            all_new.extend(feed_new)
            run_end_utc = datetime.now(timezone.utc)
            new_cursor = max_pub_dt if max_pub_dt is not None else run_end_utc
            new_state[feed_url] = new_cursor.isoformat()
            continue

        # --- RSS / Atom path ---
        resp = _fetch_with_retry(feed_url, feed_name)
        if resp is None:
            continue  # logged inside _fetch_with_retry; do not update cursor

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            print(f"[ERROR] XML parse error for '{feed_name}': {exc} — skipping")
            continue

        entries, is_atom = _parse_entries(root)

        # Track max published datetime across ALL entries to set the cursor.
        # We scan all entries regardless of inclusion so that capped items
        # (beyond max_items) also advance the cursor and won't re-appear.
        max_pub_dt: Optional[datetime] = None
        feed_new: list = []
        included = 0
        topic = feed_cfg.get("topic") or "industry"

        for entry in entries:
            title, url, raw, date_str = _entry_fields(entry, is_atom)

            # Skip entries missing title or link (malformed)
            if not title or not url:
                continue

            date, pub_dt = parse_feed_date(date_str, fetched_date)

            # Advance global max for this feed (used for cursor update)
            if pub_dt is not None:
                if max_pub_dt is None or pub_dt > max_pub_dt:
                    max_pub_dt = pub_dt

            # --- Inclusion decision ---

            # Cursor filter: skip items at or before the last cursor
            if cursor_dt is not None and pub_dt is not None and pub_dt <= cursor_dt:
                continue

            # URL dedup: skip if the exact URL is already in industry.json
            if url in existing_urls:
                print(f"[INFO]   SKIP (dup URL): {title[:70]}")
                continue

            # Respect per-feed max_items cap
            if included >= max_items:
                continue

            abstract = strip_html(raw)
            feed_new.append({
                "title":        title,
                "abstract":     abstract,
                "url":          url,
                "topic":        topic,
                "date":         date,
                "fetched_date": fetched_date,
                "feed_name":    feed_name,
                "source":       "industry",
            })
            existing_urls.add(url)
            included += 1
            print(f"[INFO]   NEW [{feed_name}]: {title[:70]}")

        all_new.extend(feed_new)
        print(f"[INFO] [{feed_name}] {included} new item(s) ingested")

        # Update cursor to max published across ALL entries in this XML
        # (see module docstring for the exact rule)
        run_end_utc = datetime.now(timezone.utc)
        new_cursor = max_pub_dt if max_pub_dt is not None else run_end_utc
        new_state[feed_url] = new_cursor.isoformat()

    return all_new, new_state
