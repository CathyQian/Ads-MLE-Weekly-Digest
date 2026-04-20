# ArXiv Summarizer — Project Guide

## What This Project Does

Fetches research papers from arXiv, stores them in `docs/papers.json`, and publishes a static website with an RSS feed. Runs automatically every Friday via GitHub Actions. No AI summarization — abstracts are displayed as-is from the arXiv API.

## Scripts

| Script | Use Case | AI Provider |
|---|---|---|
| `weekly_runner.py` | Automated weekly job — updates site + RSS | None |
| `industry_feeds.py` | Library: fetch/parse industry RSS/Atom feeds | None |

## Configuration (`config.yaml`)

| Key | Default | Description |
|---|---|---|
| `max_results_per_keyword` | `10` | Papers fetched per keyword per run |
| `categories` | `["cs.*"]` | arXiv category filters (e.g. add `"stat.ML"` to include stats papers) |
| `site_url` | `""` | Public URL of the site (used in RSS `<link>` and `<atom:link>`) |
| `keywords` | — | List of search terms sent to the arXiv API |
| `industry_feeds` | `[]` | List of RSS/Atom feed configs (see below) |

### `industry_feeds` entry fields

| Field | Required | Description |
|---|---|---|
| `name` | yes | Human-readable feed name shown as a badge on each card |
| `url` | yes | Full URL of the RSS 2.0 or Atom 1.0 feed |
| `max_items` | no (default 10) | Maximum new items to ingest per run |
| `topic` | no (default "industry") | Topic slug for filtering — see list below |

**Topic slugs:** `industry-blog`, `ads-tech`, `ml-engineering`, `llm`, `infra`

## Constants (in `weekly_runner.py`)

Tunable without touching logic:

| Constant | Value | Meaning |
|---|---|---|
| `LOOKBACK_DAYS` | `7` | Days of arXiv history queried each run |
| `MAX_RETRIES` | `3` | HTTP retry attempts for arXiv requests |
| `RSS_LIMIT` | `50` | Max items in `feed.xml` |
| `INTER_KEYWORD_SLEEP` | `3` | Seconds between keyword requests (avoids 429s) |
| `MAX_RESULTS_DEFAULT` | `10` | Fallback if `max_results_per_keyword` not in config |

## Paper Schema (`Paper` TypedDict)

```python
class Paper(TypedDict):
    title:        str  # paper title, newlines stripped
    abstract:     str  # full abstract text
    url:          str  # canonical arXiv URL, no version suffix
    pdf:          str  # direct PDF link
    keyword:      str  # config keyword that matched this paper
    date:         str  # YYYY-MM-DD arXiv published date
    fetched_date: str  # YYYY-MM-DD date this weekly run executed
```

URLs are normalized (version suffix stripped) before storage so `v1` and `v2` of the same paper don't create duplicate entries.

## Required Environment Variables

`weekly_runner.py` requires no API keys — it only calls the free arXiv Atom API.

## Running Locally

```bash
pip install -r requirements.txt

# Dry run: fetch + print, no files written
python3 weekly_runner.py --dry-run

# Full run: fetch, save papers.json, regenerate index.html + feed.xml
python3 weekly_runner.py
```

## Website & RSS

- **Site:** `https://adsmachinelearning.com`
- **RSS feed:** `https://adsmachinelearning.com/feed.xml`
- Papers grouped by weekly run date, newest run at top.
- RSS includes the latest 50 papers with full abstract.

### Enabling GitHub Pages

1. Go to **Settings → Pages** in the repo.
2. Source: **Deploy from a branch** → branch `main` → folder `/docs`.
3. Save.

## Automated Workflow

`.github/workflows/daily_arxiv.yml` runs every Friday at 9 PM ET:
1. Runs `weekly_runner.py` — fetches new papers, appends to `docs/papers.json`
2. Regenerates `docs/index.html` and `docs/feed.xml`
3. Commits and pushes the updated `docs/` folder

No secrets needed in GitHub Actions — `weekly_runner.py` uses only the public arXiv API.

## Data Persistence

**arXiv** — `docs/papers.json` (unchanged schema):
- Loads existing papers, deduplicates by normalized URL
- Atomically appends new papers; crash mid-write leaves previous file intact
- Regenerates `index.html` and `feed.xml` from the full accumulated dataset

**Industry feeds** — `docs/industry.json`:
- Schema: `title`, `abstract` (plain text), `url`, `topic`, `date` (YYYY-MM-DD),
  `fetched_date`, `feed_name`, `source: "industry"`
- Deduplicated by exact URL within `industry.json` (same URL may exist in `papers.json` — user uses Remove button to hide it)
- Atomically written via `tempfile` + `os.replace()`

**Feed cursors** — `docs/industry_feed_state.json`:
- Maps each feed URL → `last_seen_published_iso` (UTC ISO string)
- Cursor is set to `max(published datetime across ALL entries in the fetched XML)` after each successful run, so old items aren't re-ingested even if they hit the `max_items` cap
- If no cursor exists (first run): includes up to `max_items` items with no date filter
- Items with unparseable dates are always included (subject to URL dedup and `max_items`)

**HTML** — merges arXiv papers + industry items into a unified timeline.
**RSS (`feed.xml`)** — arXiv only; industry items are never added to the feed.

## Running Tests

```bash
pip install -r requirements.txt   # includes pytest
python3 -m pytest tests/ -v
```
