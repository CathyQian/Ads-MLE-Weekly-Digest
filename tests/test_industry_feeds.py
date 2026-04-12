"""Tests for industry_feeds.py"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow importing from repo root without installing
sys.path.insert(0, str(Path(__file__).parent.parent))
from industry_feeds import (
    fetch_industry_feeds,
    load_feed_state,
    parse_feed_date,
    strip_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_unescapes_entities():
    assert strip_html("AT&amp;T") == "AT&T"
    assert strip_html("&lt;b&gt;") == "<b>"


def test_strip_html_collapses_whitespace():
    assert strip_html("  foo   bar  ") == "foo bar"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_feed_date
# ---------------------------------------------------------------------------

def test_parse_feed_date_atom_z():
    date, dt = parse_feed_date("2026-04-12T10:00:00Z", "2000-01-01")
    assert date == "2026-04-12"
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026


def test_parse_feed_date_atom_offset():
    date, dt = parse_feed_date("2026-04-12T10:00:00+05:30", "2000-01-01")
    assert date == "2026-04-12"
    assert dt is not None


def test_parse_feed_date_rss_rfc2822():
    date, dt = parse_feed_date("Sat, 12 Apr 2026 10:00:00 +0000", "2000-01-01")
    assert date == "2026-04-12"
    assert dt is not None


def test_parse_feed_date_invalid_returns_fallback():
    date, dt = parse_feed_date("not-a-date", "2026-01-01")
    assert date == "2026-01-01"
    assert dt is None


def test_parse_feed_date_empty_returns_fallback():
    date, dt = parse_feed_date("", "2026-01-01")
    assert date == "2026-01-01"
    assert dt is None


# ---------------------------------------------------------------------------
# fetch_industry_feeds — RSS 2.0
# ---------------------------------------------------------------------------

def _make_mock_response(xml_path: Path) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.content = xml_path.read_bytes()
    return mock


def test_fetch_rss_parses_items():
    """RSS fixture has 2 valid items (2 others lack title/link and are skipped)."""
    cfg = [{"url": "https://example.com/feed.xml", "name": "Test", "topic": "test", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        items, state = fetch_industry_feeds(cfg, "2026-04-12", {}, set())

    assert len(items) == 2
    titles = {i["title"] for i in items}
    assert "New Article About Ranking" in titles
    assert "Old Article" in titles

    # HTML was stripped from description
    ranking = next(i for i in items if "Ranking" in i["title"])
    assert "<p>" not in ranking["abstract"]
    assert "new ranking" in ranking["abstract"]

    # Metadata
    assert items[0]["source"] == "industry"
    assert items[0]["topic"] == "test"
    assert items[0]["feed_name"] == "Test"


def test_fetch_rss_skips_missing_title_or_link():
    cfg = [{"url": "https://example.com/feed.xml", "name": "Test", "topic": "test", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        items, _ = fetch_industry_feeds(cfg, "2026-04-12", {}, set())
    urls = {i["url"] for i in items}
    assert "https://example.com/no-title" not in urls
    # no-link item has empty url so it's also skipped
    assert "" not in urls


# ---------------------------------------------------------------------------
# fetch_industry_feeds — Atom 1.0
# ---------------------------------------------------------------------------

def test_fetch_atom_parses_items():
    """Atom fixture has 2 valid items (1 has empty title and is skipped)."""
    cfg = [{"url": "https://example.com/atom", "name": "Atom", "topic": "atom-test", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_atom.xml")):
        items, state = fetch_industry_feeds(cfg, "2026-04-12", {}, set())

    assert len(items) == 2
    titles = {i["title"] for i in items}
    assert "Atom Article on Recommendation" in titles
    assert "Older Atom Article" in titles

    rec = next(i for i in items if "Recommendation" in i["title"])
    assert "<em>" not in rec["abstract"]
    assert "innovative" in rec["abstract"]
    assert rec["date"] == "2026-04-12"


# ---------------------------------------------------------------------------
# Cursor filtering
# ---------------------------------------------------------------------------

def test_cursor_filters_old_items():
    """Items at or before the cursor datetime must be excluded."""
    # cursor = 2026-01-01T00:00:00+00:00 → only 2026-04-12 item passes
    cursor = "2026-01-01T00:00:00+00:00"
    state = {"https://example.com/feed.xml": cursor}
    cfg = [{"url": "https://example.com/feed.xml", "name": "T", "topic": "t", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        items, new_state = fetch_industry_feeds(cfg, "2026-04-12", state, set())

    # Only the 2026-04-12 item is newer than cursor; 2024-01-01 is filtered
    assert len(items) == 1
    assert "Ranking" in items[0]["title"]


def test_cursor_updated_to_max_published():
    """After fetch, cursor must equal max published in the feed XML."""
    cfg = [{"url": "https://example.com/feed.xml", "name": "T", "topic": "t", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        _, new_state = fetch_industry_feeds(cfg, "2026-04-12", {}, set())

    cursor_str = new_state.get("https://example.com/feed.xml", "")
    assert cursor_str != ""
    cursor_dt = datetime.fromisoformat(cursor_str)
    # max published in RSS fixture is 2026-04-12T10:00:00Z
    assert cursor_dt.year == 2026
    assert cursor_dt.month == 4
    assert cursor_dt.day == 12


def test_first_run_no_cursor_includes_up_to_max():
    """On first run (no cursor) all valid items up to max_items are included."""
    cfg = [{"url": "https://example.com/feed.xml", "name": "T", "topic": "t", "max_items": 1}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        items, _ = fetch_industry_feeds(cfg, "2026-04-12", {}, set())
    assert len(items) == 1  # capped at max_items=1


# ---------------------------------------------------------------------------
# URL deduplication
# ---------------------------------------------------------------------------

def test_url_dedup_skips_existing_industry_url():
    """URL already in existing_urls must be skipped."""
    existing = {"https://example.com/ranking-2026"}
    cfg = [{"url": "https://example.com/feed.xml", "name": "T", "topic": "t", "max_items": 10}]
    with patch("industry_feeds.requests.get", return_value=_make_mock_response(FIXTURES / "sample_rss.xml")):
        items, _ = fetch_industry_feeds(cfg, "2026-04-12", {}, existing)
    urls = {i["url"] for i in items}
    assert "https://example.com/ranking-2026" not in urls


# ---------------------------------------------------------------------------
# Network failure — silent skip
# ---------------------------------------------------------------------------

def test_feed_failure_is_silent():
    """A failing feed must not raise; other feeds still proceed."""
    bad_resp = MagicMock()
    bad_resp.status_code = 500

    cfg = [
        {"url": "https://bad.example.com/feed.xml", "name": "Bad", "topic": "t", "max_items": 10},
        {"url": "https://example.com/feed.xml",     "name": "Good", "topic": "t", "max_items": 10},
    ]

    def side_effect(url, **kwargs):
        if "bad" in url:
            return bad_resp
        return _make_mock_response(FIXTURES / "sample_rss.xml")

    with patch("industry_feeds.requests.get", side_effect=side_effect):
        items, _ = fetch_industry_feeds(cfg, "2026-04-12", {}, set())

    assert len(items) == 2  # only items from the good feed
