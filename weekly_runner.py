import argparse
import os
import re
import sys
import json
import time
import tempfile
import yaml
import requests
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from typing import TypedDict
from xml.sax.saxutils import escape


# ---------------------------------------------------------------------------
# Paper schema
# ---------------------------------------------------------------------------

class Paper(TypedDict):
    title:        str   # paper title, newlines stripped
    abstract:     str   # full abstract text
    url:          str   # canonical arXiv URL, no version suffix
    pdf:          str   # direct PDF link
    keyword:      str   # config keyword that matched this paper
    date:         str   # YYYY-MM-DD arXiv published date
    fetched_date: str   # YYYY-MM-DD date this weekly run executed
    authors:      list  # list of "Name (Affiliation)" strings; affiliation omitted if unavailable
    article_type: str   # "review" or "research" (inferred from title/abstract)


# ---------------------------------------------------------------------------
# Constants  (override via config.yaml where noted)
# ---------------------------------------------------------------------------

LOOKBACK_DAYS       = 7    # days of arXiv history to query each run
MAX_RETRIES         = 3    # arXiv HTTP request retries
RSS_LIMIT           = 50   # max items in feed.xml
INTER_KEYWORD_SLEEP = 3    # seconds between keyword requests (rate limiting)
MAX_RESULTS_DEFAULT = 10   # fallback max_results_per_keyword

ATOM_NS          = "{http://www.w3.org/2005/Atom}"
ARXIV_NS         = "{http://arxiv.org/schemas/atom}"  # for <arxiv:affiliation>
DOCS_DIR         = "docs"
PAPERS_JSON      = os.path.join(DOCS_DIR, "papers.json")
INDUSTRY_JSON    = os.path.join(DOCS_DIR, "industry.json")
INDUSTRY_STATE   = os.path.join(DOCS_DIR, "industry_feed_state.json")
STARTUP_JSON     = os.path.join(DOCS_DIR, "startup.json")
STARTUP_STATE    = os.path.join(DOCS_DIR, "startup_feed_state.json")
INDEX_HTML       = os.path.join(DOCS_DIR, "index.html")
FEED_XML         = os.path.join(DOCS_DIR, "feed.xml")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Gemini summarization
# ---------------------------------------------------------------------------

def summarize_with_gemini(text: str) -> str:
    """Call Gemini 1.5 Flash to summarize text. Returns original text on failure."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not text.strip():
        return text
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    prompt = (
        "Summarize the following article in 2-3 sentences, focusing on what it "
        "means for ML practitioners and startup builders:\n\n" + text[:3000]
    )
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        print(f"[WARN]  Gemini returned {resp.status_code} — using original text")
    except Exception as e:
        print(f"[WARN]  Gemini error: {e} — using original text")
    return text


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    """Strip arXiv version suffix (v1, v2, …) for stable deduplication."""
    return re.sub(r'v\d+$', '', url.strip())


_REVIEW_PATTERNS = re.compile(
    r'\b(survey|review|overview|tutorial|comprehensive\s+study|systematic\s+review|'
    r'literature\s+review|position\s+paper|benchmark\s+study|comparison\s+of|'
    r'we\s+survey|this\s+survey|this\s+review|this\s+paper\s+(surveys|reviews|overviews|summarizes))\b',
    re.IGNORECASE,
)

def _infer_article_type(title: str, abstract: str) -> str:
    """Return 'review' if the paper is a survey/review, otherwise 'research'."""
    if _REVIEW_PATTERNS.search(title) or _REVIEW_PATTERNS.search(abstract[:500]):
        return "review"
    return "research"


_INDUSTRY_PATTERN = re.compile(
    r'\b(google|deepmind|google[\s\-]brain|google[\s\-]research|'
    r'meta[\s\-]ai|meta\b|facebook|instagram|'
    r'kuaishou|'
    r'alibaba|alipay|ant[\s\-]group|taobao|tmall|'
    r'tencent|wechat|'
    r'uber|'
    r'airbnb|'
    r'pinterest|'
    r'microsoft|msra|azure|'
    r'apple[\s\-](?:inc|research)|'
    r'amazon|aws|'
    r'netflix|'
    r'bytedance|tiktok|'
    r'baidu|'
    r'jd[\s\-](?:com|ai)|'
    r'linkedin|'
    r'salesforce|'
    r'nvidia|'
    r'openai|'
    r'anthropic|'
    r'twitter|'
    r'snap[\s\-]research|snapchat|'
    r'spotify|'
    r'adobe[\s\-]research|'
    r'huawei|'
    r'samsung[\s\-]research|'
    r'didi|'
    r'meituan)\b',
    re.IGNORECASE,
)

_INDUSTRY_DISPLAY = {
    "google": "Google", "deepmind": "DeepMind",
    "meta": "Meta", "facebook": "Meta", "instagram": "Meta",
    "kuaishou": "Kuaishou", "alibaba": "Alibaba",
    "alipay": "Alibaba", "ant": "Ant Group",
    "tencent": "Tencent", "wechat": "Tencent",
    "uber": "Uber", "airbnb": "Airbnb", "pinterest": "Pinterest",
    "microsoft": "Microsoft", "msra": "Microsoft",
    "apple": "Apple", "amazon": "Amazon", "aws": "Amazon",
    "netflix": "Netflix", "bytedance": "ByteDance",
    "tiktok": "ByteDance", "baidu": "Baidu",
    "jd": "JD.com", "linkedin": "LinkedIn",
    "salesforce": "Salesforce", "nvidia": "NVIDIA",
    "openai": "OpenAI", "anthropic": "Anthropic",
    "twitter": "Twitter/X", "snap": "Snap",
    "snapchat": "Snap", "spotify": "Spotify",
    "adobe": "Adobe", "huawei": "Huawei",
    "samsung": "Samsung", "didi": "DiDi",
    "meituan": "Meituan",
}

_CO = _INDUSTRY_PATTERN.pattern  # shorthand for building compound patterns

# Affiliation signals in abstract text — each catches a different surface form:
_AFFIL_PATTERNS = [
    # 1. Preposition: "at Google", "from Tencent", "with Meta", "@ Alibaba"
    re.compile(r'(?:at|from|with|@)\s+(?:' + _CO + r')\b', re.IGNORECASE),

    # 2. Company + org-type word: "Google Research", "Tencent AI Lab",
    #    "DeepMind Brain", "Alibaba Group", "ByteDance Inc"
    re.compile(r'(?:' + _CO + r')\s+(?:research|labs?|ai|inc\.?|corp\.?|'
               r'technologies?|group|brain|cloud|systems?|platform|'
               r'holdings?|studio)', re.IGNORECASE),

    # 3. Parenthesised affiliation: "(Google)", "(Tencent AI)"  — common in
    #    author footnotes that leak into the abstract field
    re.compile(r'\(\s*(?:' + _CO + r')\s*[,)]', re.IGNORECASE),

    # 4. Footnote-marker + company: "¹Google", "2Tencent", "*Meta AI",
    #    "†Alibaba" — superscript/footnote affiliation lines
    re.compile(r'(?:^|[\n,;])\s*[\d¹²³⁴⁵*†‡§]+\s*(?:' + _CO + r')\b',
               re.IGNORECASE | re.MULTILINE),

    # 5. Company followed by a city/country (location-style footnote):
    #    "Google, Mountain View", "Tencent, Shenzhen", "Meta, Menlo Park"
    #    Require a capital letter after the comma to reduce false positives.
    re.compile(r'(?:' + _CO + r'),\s+[A-Z][a-zA-Z]{2,}', re.IGNORECASE),

    # 6. Company possessive: "Kuaishou's platform", "Tencent's system"
    #    Strong signal that the paper is about their own production system.
    re.compile(r'(?:' + _CO + r')\'s\b', re.IGNORECASE),

    # 7. Company as sentence subject doing something industrial:
    #    "Kuaishou serves", "Tencent deploys", "Google processes"
    #    Matches company name at/near sentence start followed by a verb.
    re.compile(
        r'(?:^|(?<=\. ))\s*(?:' + _CO + r')\s+'
        r'(?:serves?|deploys?|processes?|operates?|launches?|presents?|'
        r'introduces?|develops?|builds?|has\b|is\b|provides?|hosts?)',
        re.IGNORECASE | re.MULTILINE,
    ),
]


def _extract_company(authors: list, abstract: str = "") -> str:
    """Return the first matched company name from author affiliations or abstract.

    Checks (in order):
    1. Author XML affiliation strings (arXiv <arxiv:affiliation> tags).
    2. Abstract text using multiple surface-form patterns covering prepositions
       ('at Google'), org-type words ('Tencent Research'), parenthesised
       affiliations ('(Meta AI)'), footnote markers ('¹Alibaba'), and
       location-style footnotes ('Google, Mountain View').
    """
    # 1. Author XML affiliations (highest confidence)
    for text in authors:
        m = _INDUSTRY_PATTERN.search(text)
        if m:
            raw = m.group(0)
            key = raw.split()[0].lower().rstrip("-")
            return _INDUSTRY_DISPLAY.get(key, raw.title())

    # 2. Abstract — try each surface-form pattern in turn
    if abstract:
        for pat in _AFFIL_PATTERNS:
            m = pat.search(abstract[:1200])
            if m:
                inner = _INDUSTRY_PATTERN.search(m.group(0))
                if inner:
                    raw = inner.group(0)
                    key = raw.split()[0].lower().rstrip("-")
                    return _INDUSTRY_DISPLAY.get(key, raw.title())

    return ""


# ---------------------------------------------------------------------------
# arXiv fetch
# ---------------------------------------------------------------------------

def fetch_papers(keyword: str, start_date: str, end_date: str,
                 max_results: int, categories: list = None,
                 retries: int = MAX_RETRIES) -> list:
    """
    Fetch papers from arXiv Atom API.

    start_date / end_date: YYYYMMDD (required by arXiv submittedDate filter).
    categories: list of arXiv category globs, e.g. ["cs.*", "stat.ML"].
    """
    cats = categories or ["cs.*"]
    cat_filter = "+OR+".join(f"cat:{c}" for c in cats)
    query = f'all:"{keyword}"+AND+({cat_filter})'
    url = (
        f"https://export.arxiv.org/api/query?"
        f"search_query=({query})+AND+submittedDate:[{start_date}+TO+{end_date}]"
        f"&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
    )

    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=30)
        except requests.RequestException as e:
            print(f"[ERROR] arXiv request failed for '{keyword}' (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            continue

        if response.status_code == 200:
            break
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 2 ** attempt))
            print(f"[WARN]  arXiv rate limited for '{keyword}', retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"[ERROR] arXiv returned {response.status_code} for '{keyword}' (attempt {attempt + 1})")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    else:
        return []

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        print(f"[ERROR] Failed to parse arXiv XML for '{keyword}': {e}")
        return []

    papers = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        try:
            paper_id  = entry.find(f"{ATOM_NS}id").text.strip()
            title     = entry.find(f"{ATOM_NS}title").text.strip().replace("\n", " ")
            abstract  = entry.find(f"{ATOM_NS}summary").text.strip()
            pdf_link  = entry.find(f"{ATOM_NS}link[@title='pdf']")
            pdf_url   = pdf_link.attrib["href"] if pdf_link is not None else paper_id.replace("/abs/", "/pdf/")
            pub_tag   = entry.find(f"{ATOM_NS}published")
            published = pub_tag.text.strip()[:10] if pub_tag is not None else ""

            authors = []
            for author in entry.findall(f"{ATOM_NS}author"):
                name_tag = author.find(f"{ATOM_NS}name")
                if name_tag is None:
                    continue
                name = name_tag.text.strip()
                aff_tag = author.find(f"{ARXIV_NS}affiliation")
                if aff_tag is not None and aff_tag.text and aff_tag.text.strip():
                    authors.append(f"{name} ({aff_tag.text.strip()})")
                else:
                    authors.append(name)

            papers.append({
                "title":        title,
                "abstract":     abstract,
                "url":          _normalize_url(paper_id),
                "pdf":          pdf_url,
                "keyword":      keyword,
                "date":         published,
                "fetched_date": "",  # filled in by main()
                "authors":      authors,
                "article_type": _infer_article_type(title, abstract),
            })
        except (AttributeError, KeyError) as e:
            print(f"[WARN]  Skipping malformed entry for '{keyword}': {e}")
            continue

    return papers


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_existing_papers() -> list:
    if not os.path.exists(PAPERS_JSON):
        return []
    with open(PAPERS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_papers(papers: list) -> None:
    """Atomically write papers.json using a temp file + os.replace().

    A crash mid-write leaves the previous file intact.
    """
    os.makedirs(DOCS_DIR, exist_ok=True)
    dir_ = os.path.abspath(DOCS_DIR)
    with tempfile.NamedTemporaryFile(
        "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(papers, f, indent=2, ensure_ascii=False)
        tmp_path = f.name
    os.replace(tmp_path, PAPERS_JSON)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _paper_card(p: dict) -> str:
    url = p['url']
    authors = p.get("authors") or []
    authors_html = (
        f'<p class="authors">{escape(", ".join(authors))}</p>'
        if authors else ""
    )
    atype = p.get("article_type", "research")
    type_badge = f'<span class="type-badge type-{escape(atype)}">{escape(atype)}</span>'
    company = _extract_company(authors, p.get("abstract", ""))
    company_badge = (
        f'<span class="company-badge">{escape(company)}</span>' if company else ""
    )
    industry_class = " industry-paper" if company else ""
    industry_attr  = "1" if company else "0"
    return f"""
    <article class="card{industry_class}" data-keyword="{escape(p['keyword'])}" data-url="{url}" data-type="{escape(atype)}" data-industry="{industry_attr}" data-source="arxiv">
      <div class="card-meta">
        <span class="date">{p.get('date', '')}</span>
        <span class="keyword editable-keyword" onclick="openKwEdit(event,this)">{escape(p['keyword'])} <span class="edit-hint">&#9662;</span></span>
        {type_badge}
        {company_badge}
        <div class="card-actions">
          <button class="btn-star"   onclick="toggleStar(this)" title="Star this paper">&#9733;</button>
          <button class="btn-keep"   onclick="setStatus(this, 'kept')">&#10003; Keep</button>
          <button class="btn-remove" onclick="setStatus(this, 'removed')">&#10005; Remove</button>
        </div>
      </div>
      <h2 class="card-title">
        <a href="{url}" target="_blank" rel="noopener">{escape(p['title'])}</a>
      </h2>
      {authors_html}
      <p class="abstract">{escape(p['abstract'])}</p>
      <div class="card-links">
        <a href="{url}" target="_blank" rel="noopener">Abstract</a>
        <a href="{p['pdf']}" target="_blank" rel="noopener">PDF</a>
      </div>
    </article>"""


def _startup_card(item: dict) -> str:
    url = item["url"]
    feed_badge = f'<span class="feed-badge startup-badge">{escape(item.get("feed_name", ""))}</span>'
    topic = item.get("topic", "startup")
    return f"""
    <article class="card startup-item" data-keyword="{escape(topic)}" data-url="{url}" data-type="startup" data-source="startup">
      <div class="card-meta">
        <span class="date">{item.get('date', '')}</span>
        <span class="keyword">{escape(topic)}</span>
        {feed_badge}
        <div class="card-actions">
          <button class="btn-star"   onclick="toggleStar(this)" title="Star">&#9733;</button>
          <button class="btn-keep"   onclick="setStatus(this, 'kept')">&#10003; Keep</button>
          <button class="btn-remove" onclick="setStatus(this, 'removed')">&#10005; Remove</button>
        </div>
      </div>
      <h2 class="card-title">
        <a href="{url}" target="_blank" rel="noopener">{escape(item['title'])}</a>
      </h2>
      <p class="abstract">{escape(item.get('abstract', ''))}</p>
      <div class="card-links">
        <a href="{url}" target="_blank" rel="noopener">Read article &#8594;</a>
      </div>
    </article>"""


def _industry_card(item: dict) -> str:
    url = item["url"]
    feed_badge = f'<span class="feed-badge">{escape(item.get("feed_name", ""))}</span>'
    topic = item.get("topic", "industry")
    return f"""
    <article class="card industry-item" data-keyword="{escape(topic)}" data-url="{url}" data-type="industry" data-industry="1" data-source="industry">
      <div class="card-meta">
        <span class="date">{item.get('date', '')}</span>
        <span class="keyword">{escape(topic)}</span>
        {feed_badge}
        <div class="card-actions">
          <button class="btn-star"   onclick="toggleStar(this)" title="Star">&#9733;</button>
          <button class="btn-keep"   onclick="setStatus(this, 'kept')">&#10003; Keep</button>
          <button class="btn-remove" onclick="setStatus(this, 'removed')">&#10005; Remove</button>
        </div>
      </div>
      <h2 class="card-title">
        <a href="{url}" target="_blank" rel="noopener">{escape(item['title'])}</a>
      </h2>
      <p class="abstract">{escape(item.get('abstract', ''))}</p>
      <div class="card-links">
        <a href="{url}" target="_blank" rel="noopener">Read article &#8594;</a>
      </div>
    </article>"""


def generate_html(papers: list, industry_items: list = None, startup_items: list = None) -> str:
    industry_items = industry_items or []
    startup_items  = startup_items  or []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # --- ArXiv panel ---
    arxiv_groups: dict = defaultdict(list)
    for p in papers:
        arxiv_groups[p.get("fetched_date") or p.get("date", "unknown")].append(p)

    arxiv_sections = []
    for run_date, grp in sorted(arxiv_groups.items(), key=lambda x: x[0], reverse=True):
        co  = sorted([p for p in grp if _extract_company(p.get("authors") or [], p.get("abstract", ""))],
                     key=lambda p: p.get("date", ""), reverse=True)
        rest = sorted([p for p in grp if not _extract_company(p.get("authors") or [], p.get("abstract", ""))],
                      key=lambda p: p.get("date", ""), reverse=True)
        cards = "".join(_paper_card(p) for p in co + rest)
        arxiv_sections.append(f"""
  <section class="run-group">
    <h2 class="run-header">Week of {run_date}</h2>
    {cards}
  </section>""")
    arxiv_body = "\n".join(arxiv_sections) if arxiv_sections else '<p class="empty">No papers yet. Check back after the next weekly run.</p>'

    arxiv_kw_options = "\n".join(
        f'      <option value="{escape(kw)}">{escape(kw)}</option>'
        for kw in sorted({p["keyword"] for p in papers})
    )

    # --- Industry panel ---
    ind_groups: dict = defaultdict(list)
    for item in industry_items:
        ind_groups[item.get("fetched_date") or item.get("date", "unknown")].append(item)

    ind_sections = []
    for run_date, grp in sorted(ind_groups.items(), key=lambda x: x[0], reverse=True):
        cards = "".join(_industry_card(i) for i in sorted(grp, key=lambda i: i.get("date", ""), reverse=True))
        ind_sections.append(f"""
  <section class="run-group">
    <h2 class="run-header">Week of {run_date}</h2>
    {cards}
  </section>""")
    ind_body = "\n".join(ind_sections) if ind_sections else '<p class="empty">No industry news yet.</p>'

    ind_topic_options = "\n".join(
        f'      <option value="{escape(t)}">{escape(t)}</option>'
        for t in sorted({i.get("topic", "industry") for i in industry_items})
    )

    # --- Startup panel ---
    su_groups: dict = defaultdict(list)
    for item in startup_items:
        su_groups[item.get("fetched_date") or item.get("date", "unknown")].append(item)

    su_sections = []
    for run_date, grp in sorted(su_groups.items(), key=lambda x: x[0], reverse=True):
        cards = "".join(_startup_card(i) for i in sorted(grp, key=lambda i: i.get("date", ""), reverse=True))
        su_sections.append(f"""
  <section class="run-group">
    <h2 class="run-header">Week of {run_date}</h2>
    {cards}
  </section>""")
    su_body = "\n".join(su_sections) if su_sections else '<p class="empty">No startup news yet.</p>'

    su_topic_options = "\n".join(
        f'      <option value="{escape(t)}">{escape(t)}</option>'
        for t in sorted({i.get("topic", "startup") for i in startup_items})
    )

    total_arxiv    = len(papers)
    total_industry = len(industry_items)
    total_startup  = len(startup_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ArXiv And News Weekly Digest</title>
  <link rel="alternate" type="application/rss+xml" title="ArXiv And News Weekly Digest RSS" href="feed.xml">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f7fa; color: #222; line-height: 1.6;
    }}
    header {{
      background: #1a73e8; color: #fff; padding: 20px 24px;
      display: flex; align-items: center; justify-content: space-between;
    }}
    header h1 {{ font-size: 1.4rem; font-weight: 700; }}
    header p  {{ font-size: 0.85rem; opacity: 0.85; margin-top: 2px; }}
    .tab-bar {{
      background: #1565c0; display: flex; padding: 0 24px; gap: 0;
    }}
    .tab {{
      color: rgba(255,255,255,.7); background: none; border: none;
      border-bottom: 3px solid transparent; padding: 12px 22px;
      font-size: 0.9rem; font-weight: 500; cursor: pointer;
      transition: all .15s; letter-spacing: 0.01em;
    }}
    .tab:hover {{ color: rgba(255,255,255,.9); }}
    .tab.active {{ color: #fff; border-bottom-color: #fff; }}
    .tab-panel {{ max-width: 860px; margin: 24px auto; padding: 0 16px; }}
    .toolbar {{
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
      margin-bottom: 16px;
    }}
    .toolbar label {{ font-size: 0.85rem; color: #555; white-space: nowrap; }}
    .filter-select {{
      appearance: none;
      background: #fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%231a73e8' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E") no-repeat right 12px center;
      border: 1px solid #c5d0e8; border-radius: 20px;
      padding: 6px 36px 6px 14px; font-size: 0.85rem; color: #222;
      cursor: pointer; min-width: 200px; outline: none;
    }}
    .filter-select:focus {{ border-color: #1a73e8; box-shadow: 0 0 0 2px rgba(26,115,232,0.15); }}
    .starred-btn {{
      margin-left: auto; font-size: 0.8rem; color: #888; background: none;
      border: 1px solid #ccc; border-radius: 14px; padding: 4px 12px; cursor: pointer;
    }}
    .starred-btn:hover {{ color: #444; border-color: #999; }}
    .stats {{ font-size: 0.85rem; color: #666; margin-bottom: 20px; }}
    .card {{
      background: #fff; border: 1px solid #e0e6f0; border-radius: 10px;
      padding: 20px 24px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
      transition: border-color .15s;
    }}
    .card.kept    {{ border-color: #34a853; }}
    .card.removed {{ opacity: .45; }}
    .card-meta {{ display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }}
    .date {{ font-size: 0.82rem; color: #888; }}
    .keyword {{
      font-size: 0.75rem; background: #e8f0fe; color: #1a73e8;
      padding: 2px 10px; border-radius: 12px; font-weight: 500;
    }}
    .editable-keyword {{ cursor: pointer; position: relative; user-select: none; }}
    .editable-keyword:hover {{ background: #c7d9fc; }}
    .edit-hint {{ font-size: 0.65rem; opacity: 0.7; }}
    .kw-popup {{
      position: absolute; top: calc(100% + 4px); left: 0; z-index: 100;
      background: #fff; border: 1px solid #c5d0e8; border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 6px; min-width: 220px;
    }}
    .kw-popup select {{
      width: 100%; border: 1px solid #c5d0e8; border-radius: 6px;
      padding: 4px 8px; font-size: 0.8rem; color: #222; outline: none; max-height: 200px;
    }}
    .kw-overridden {{ background: #fff3cd; color: #856404; }}
    .type-badge {{
      font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
      font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
    }}
    .type-research {{ background: #e6f4ea; color: #1e8e3e; }}
    .type-review   {{ background: #fce8e6; color: #d93025; }}
    .company-badge {{
      font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
      background: #f3e8ff; color: #7b2ff7; font-weight: 600;
    }}
    .industry-paper {{ border-left: 3px solid #7b2ff7; }}
    .feed-badge {{
      font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
      background: #e3f2fd; color: #1565c0; font-weight: 500;
    }}
    .industry-item {{ border-left: 3px solid #1565c0; }}
    .startup-badge {{
      font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
      background: #ccfbf1; color: #0f766e; font-weight: 500;
    }}
    .startup-item {{ border-left: 3px solid #0d9488; }}
    .btn-star {{
      font-size: 1rem; padding: 0 4px; border: none; background: none;
      cursor: pointer; color: #ccc; line-height: 1; transition: color .15s;
    }}
    .btn-star:hover {{ color: #f9ab00; }}
    .card.starred .btn-star {{ color: #f9ab00; }}
    .card.starred {{ border-color: #f9ab00; box-shadow: 0 0 0 2px rgba(249,171,0,0.25); }}
    .card-actions {{ display: flex; gap: 6px; margin-left: auto; }}
    .btn-keep, .btn-remove {{
      font-size: 0.75rem; padding: 2px 10px; border-radius: 12px;
      border: 1px solid; cursor: pointer; background: #fff; transition: all .15s;
    }}
    .btn-keep  {{ color: #34a853; border-color: #34a853; }}
    .btn-keep:hover, .card.kept   .btn-keep   {{ background: #34a853; color: #fff; }}
    .btn-remove {{ color: #ea4335; border-color: #ea4335; }}
    .btn-remove:hover, .card.removed .btn-remove {{ background: #ea4335; color: #fff; }}
    .card-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 8px; line-height: 1.4; }}
    .card-title a {{ color: #1a1a1a; text-decoration: none; }}
    .card-title a:hover {{ color: #1a73e8; }}
    .authors  {{ font-size: 0.8rem; color: #666; margin-bottom: 8px; font-style: italic; }}
    .abstract {{ font-size: 0.88rem; color: #444; margin-bottom: 12px; line-height: 1.6; }}
    .card-links {{ display: flex; gap: 12px; }}
    .card-links a {{
      font-size: 0.82rem; color: #1a73e8; text-decoration: none;
      border: 1px solid #1a73e8; padding: 3px 12px; border-radius: 14px;
    }}
    .card-links a:hover {{ background: #1a73e8; color: #fff; }}
    .run-group  {{ margin-bottom: 40px; }}
    .run-header {{
      font-size: 1.05rem; font-weight: 700; color: #1a73e8;
      border-bottom: 2px solid #e0e6f0; padding-bottom: 6px; margin-bottom: 16px;
    }}
    .hidden {{ display: none !important; }}
    .empty  {{ color: #999; text-align: center; padding: 60px 0; }}
    footer  {{ text-align: center; font-size: 0.78rem; color: #aaa; padding: 32px 0; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>ArXiv And News Weekly Digest</h1>
      <p>Research papers and news, updated every Friday night</p>
    </div>
  </header>

  <nav class="tab-bar">
    <button class="tab active" data-tab="arxiv"    onclick="switchTab('arxiv')">ArXiv Papers</button>
    <button class="tab"        data-tab="industry" onclick="switchTab('industry')">Industry News</button>
    <button class="tab"        data-tab="startup"  onclick="switchTab('startup')">Startups</button>
  </nav>

  <!-- ArXiv panel -->
  <div id="panel-arxiv" class="tab-panel">
    <div class="toolbar">
      <label for="arxiv-kw-filter">Category:</label>
      <select id="arxiv-kw-filter" class="filter-select">
        <option value="">All categories</option>
{arxiv_kw_options}
      </select>
      <select id="arxiv-type-filter" class="filter-select" style="min-width:130px;">
        <option value="">All types</option>
        <option value="research">Research</option>
        <option value="review">Review</option>
      </select>
      <button class="starred-btn" id="toggle-starred" style="display:none;">&#9733; Starred (0)</button>
    </div>
    <p class="stats" id="arxiv-stats">{total_arxiv} papers &bull; last updated {now}</p>
    {arxiv_body}
  </div>

  <!-- Industry panel -->
  <div id="panel-industry" class="tab-panel hidden">
    <div class="toolbar">
      <label for="industry-topic-filter">Topic:</label>
      <select id="industry-topic-filter" class="filter-select">
        <option value="">All topics</option>
{ind_topic_options}
      </select>
    </div>
    <p class="stats" id="industry-stats">{total_industry} items &bull; last updated {now}</p>
    {ind_body}
  </div>

  <!-- Startup panel -->
  <div id="panel-startup" class="tab-panel hidden">
    <div class="toolbar">
      <label for="startup-topic-filter">Topic:</label>
      <select id="startup-topic-filter" class="filter-select">
        <option value="">All topics</option>
{su_topic_options}
      </select>
    </div>
    <p class="stats" id="startup-stats">{total_startup} items &bull; last updated {now}</p>
    {su_body}
  </div>

  <footer><a href="feed.xml" style="color:#aaa">RSS</a></footer>

  <script>
    // Tab switching
    function switchTab(name) {{
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
      document.getElementById('panel-' + name).classList.remove('hidden');
    }}

    // Persistence
    const STORAGE_KEY    = 'arxiv_paper_status';
    const STAR_KEY       = 'arxiv_paper_starred';
    const KW_OVERRIDE_KEY = 'arxiv_kw_overrides';
    const industryAndReviews = true;
    let showStarredOnly = false;

    function loadStatus()  {{ try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY)  || '{{}}'); }} catch {{ return {{}}; }} }}
    function saveStatus(s) {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); }}
    function loadStarred() {{ try {{ return new Set(JSON.parse(localStorage.getItem(STAR_KEY) || '[]')); }} catch {{ return new Set(); }} }}
    function saveStarred(s) {{ localStorage.setItem(STAR_KEY, JSON.stringify([...s])); }}

    function setStatus(btn, newStatus) {{
      const card = btn.closest('.card'), url = card.dataset.url;
      const st = loadStatus();
      if (st[url] === newStatus) {{ delete st[url]; card.classList.remove('kept', 'removed'); }}
      else {{ st[url] = newStatus; card.classList.remove('kept', 'removed'); card.classList.add(newStatus); }}
      saveStatus(st);
      applyArxivFilter(); applyIndustryFilter(); applyStartupFilter();
    }}

    function toggleStar(btn) {{
      const card = btn.closest('.card'), url = card.dataset.url;
      const starred = loadStarred();
      if (starred.has(url)) {{ starred.delete(url); card.classList.remove('starred'); }}
      else {{ starred.add(url); card.classList.add('starred'); }}
      saveStarred(starred);
      applyArxivFilter(); applyIndustryFilter(); applyStartupFilter();
      updateStarredButton();
    }}

    function updateStarredButton() {{
      const n = loadStarred().size;
      const btn = document.getElementById('toggle-starred');
      btn.textContent = '\u2605 Starred (' + n + ')' + (showStarredOnly ? ' \u2014 showing only' : '');
      btn.style.display = n === 0 ? 'none' : '';
      btn.style.color = showStarredOnly ? '#f9ab00' : '';
      btn.style.borderColor = showStarredOnly ? '#f9ab00' : '';
    }}

    // ArXiv filter
    function applyArxivFilter() {{
      const kw   = document.getElementById('arxiv-kw-filter').value;
      const type = document.getElementById('arxiv-type-filter').value;
      const st = loadStatus(), starred = loadStarred();
      let visible = 0;
      document.querySelectorAll('#panel-arxiv .card').forEach(card => {{
        const url    = card.dataset.url;
        const status = st[url];
        const kwMatch   = !kw   || card.dataset.keyword === kw;
        const typeMatch = !type || card.dataset.type    === type;
        const starMatch = !showStarredOnly || starred.has(url);
        const iarMatch  = !industryAndReviews || card.dataset.industry === '1' || card.dataset.type === 'review';
        const hide = !kwMatch || !typeMatch || !starMatch || !iarMatch || status === 'removed';
        card.classList.toggle('hidden', hide);
        if (!hide) visible++;
      }});
      document.querySelectorAll('#panel-arxiv .run-group').forEach(sec =>
        sec.classList.toggle('hidden', !sec.querySelector('.card:not(.hidden)')));
      const label = kw
        ? visible + ' paper' + (visible !== 1 ? 's' : '') + ' in \u201c' + kw + '\u201d'
        : visible + ' papers \u2022 last updated {now}';
      document.getElementById('arxiv-stats').textContent = label;
    }}

    // Industry filter
    function applyIndustryFilter() {{
      const topic = document.getElementById('industry-topic-filter').value;
      const st = loadStatus(), starred = loadStarred();
      let visible = 0;
      document.querySelectorAll('#panel-industry .card').forEach(card => {{
        const status = st[card.dataset.url];
        const topicMatch = !topic || card.dataset.keyword === topic;
        const starMatch  = !showStarredOnly || starred.has(card.dataset.url);
        const hide = !topicMatch || !starMatch || status === 'removed';
        card.classList.toggle('hidden', hide);
        if (!hide) visible++;
      }});
      document.querySelectorAll('#panel-industry .run-group').forEach(sec =>
        sec.classList.toggle('hidden', !sec.querySelector('.card:not(.hidden)')));
      document.getElementById('industry-stats').textContent = visible + ' items \u2022 last updated {now}';
    }}

    // Startup filter
    function applyStartupFilter() {{
      const topic = document.getElementById('startup-topic-filter').value;
      const st = loadStatus(), starred = loadStarred();
      let visible = 0;
      document.querySelectorAll('#panel-startup .card').forEach(card => {{
        const status = st[card.dataset.url];
        const topicMatch = !topic || card.dataset.keyword === topic;
        const starMatch  = !showStarredOnly || starred.has(card.dataset.url);
        const hide = !topicMatch || !starMatch || status === 'removed';
        card.classList.toggle('hidden', hide);
        if (!hide) visible++;
      }});
      document.querySelectorAll('#panel-startup .run-group').forEach(sec =>
        sec.classList.toggle('hidden', !sec.querySelector('.card:not(.hidden)')));
      document.getElementById('startup-stats').textContent = visible + ' items \u2022 last updated {now}';
    }}

    // Keyword override (arXiv panel only)
    const ALL_KWS = [...document.querySelectorAll('#arxiv-kw-filter option')]
      .map(o => o.value).filter(v => v !== '');

    function loadKwOverrides() {{ try {{ return JSON.parse(localStorage.getItem(KW_OVERRIDE_KEY) || '{{}}'); }} catch {{ return {{}}; }} }}

    function applyKwOverrides() {{
      const overrides = loadKwOverrides();
      document.querySelectorAll('#panel-arxiv .card').forEach(card => {{
        const url = card.dataset.url;
        if (overrides[url]) {{
          card.dataset.keyword = overrides[url];
          const span = card.querySelector('.editable-keyword');
          if (span) {{ span.innerHTML = overrides[url] + ' <span class="edit-hint">&#9662;</span>'; span.classList.add('kw-overridden'); }}
        }}
      }});
    }}

    function openKwEdit(event, span) {{
      event.stopPropagation();
      document.querySelectorAll('.kw-popup').forEach(p => p.remove());
      const card = span.closest('.card'), url = card.dataset.url;
      const overrides = loadKwOverrides();
      const currentKw = overrides[url] || card.dataset.keyword;
      const popup = document.createElement('div');
      popup.className = 'kw-popup';
      const select = document.createElement('select');
      select.size = Math.min(ALL_KWS.length, 8);
      ALL_KWS.forEach(kw => {{
        const opt = document.createElement('option');
        opt.value = kw; opt.textContent = kw;
        if (kw === currentKw) opt.selected = true;
        select.appendChild(opt);
      }});
      select.addEventListener('change', function () {{
        const newKw = this.value;
        const ov = loadKwOverrides();
        ov[url] = newKw;
        localStorage.setItem(KW_OVERRIDE_KEY, JSON.stringify(ov));
        card.dataset.keyword = newKw;
        span.innerHTML = newKw + ' <span class="edit-hint">&#9662;</span>';
        span.classList.add('kw-overridden');
        popup.remove();
        applyArxivFilter();
      }});
      popup.appendChild(select);
      span.appendChild(popup);
      setTimeout(() => {{
        document.addEventListener('click', function handler(e) {{
          if (!popup.contains(e.target)) {{ popup.remove(); document.removeEventListener('click', handler); }}
        }});
      }}, 0);
    }}

    // Init
    (function () {{
      const st = loadStatus(), starred = loadStarred();
      document.querySelectorAll('.card').forEach(card => {{
        const s = st[card.dataset.url];
        if (s) card.classList.add(s);
        if (starred.has(card.dataset.url)) card.classList.add('starred');
      }});
      applyKwOverrides();
      applyArxivFilter(); applyIndustryFilter(); applyStartupFilter();
      updateStarredButton();
    }})();

    document.getElementById('arxiv-kw-filter').addEventListener('change', applyArxivFilter);
    document.getElementById('arxiv-type-filter').addEventListener('change', applyArxivFilter);
    document.getElementById('industry-topic-filter').addEventListener('change', applyIndustryFilter);
    document.getElementById('startup-topic-filter').addEventListener('change', applyStartupFilter);
    document.getElementById('toggle-starred').addEventListener('click', function () {{
      showStarredOnly = !showStarredOnly;
      applyArxivFilter(); applyIndustryFilter(); applyStartupFilter();
      updateStarredButton();
    }});
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# RSS generation
# ---------------------------------------------------------------------------

def generate_rss(papers: list, site_url: str = "", rss_limit: int = RSS_LIMIT) -> str:
    sorted_papers = sorted(papers, key=lambda p: p.get("date", ""), reverse=True)
    now_rfc = formatdate(usegmt=True)

    items = []
    for p in sorted_papers[:rss_limit]:
        try:
            dt = datetime.strptime(p["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            pub_date = formatdate(dt.timestamp(), usegmt=True)
        except (ValueError, KeyError):
            pub_date = now_rfc

        description = f"<p>{escape(p['abstract'])}</p><p><a href=\"{p['pdf']}\">PDF</a></p>"

        items.append(f"""  <item>
    <title>{escape(p['title'])}</title>
    <link>{p['url']}</link>
    <guid isPermaLink="true">{p['url']}</guid>
    <pubDate>{pub_date}</pubDate>
    <category>{escape(p['keyword'])}</category>
    <description><![CDATA[{description}]]></description>
  </item>""")

    items_xml = "\n".join(items)
    feed_link = site_url.rstrip("/") + "/feed.xml" if site_url else "feed.xml"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>ArXiv And News Weekly Digest</title>
    <link>{site_url}</link>
    <description>ArXiv research papers, updated weekly.</description>
    <language>en-us</language>
    <lastBuildDate>{now_rfc}</lastBuildDate>
    <atom:link href="{feed_link}" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch and publish weekly arXiv digest.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and print results without writing any files.")
    args = parser.parse_args()

    from industry_feeds import (
        fetch_industry_feeds, load_feed_state, load_industry_items,
        save_feed_state, save_industry_items,
    )

    config          = load_config()
    keywords        = config.get("keywords", [])
    max_results     = config.get("max_results_per_keyword", MAX_RESULTS_DEFAULT)
    categories      = config.get("categories", ["cs.*"])
    site_url        = config.get("site_url", "")
    feeds_config    = config.get("industry_feeds", [])
    startup_config  = config.get("startup_feeds", [])

    blacklist   = [t.lower() for t in config.get("blacklist_terms", [])]

    if not keywords:
        print("[ERROR] No keywords found in config.yaml.")
        sys.exit(1)

    today    = datetime.utcnow()
    week_ago = today - timedelta(days=LOOKBACK_DAYS)
    start_date   = week_ago.strftime("%Y%m%d")
    end_date     = today.strftime("%Y%m%d")
    fetched_date = today.strftime("%Y-%m-%d")

    print(f"[INFO] Searching arXiv for papers from {start_date} to {end_date}")
    print(f"[INFO] Categories: {categories}")
    if args.dry_run:
        print("[INFO] --dry-run mode: no files will be written.")

    existing_papers = load_existing_papers()
    seen_urls = {_normalize_url(p["url"]) for p in existing_papers}
    new_papers = []

    for keyword in keywords:
        keyword = keyword.strip()
        fetched = fetch_papers(keyword, start_date, end_date, max_results, categories)
        print(f"[INFO] [{keyword}] Found {len(fetched)} papers")

        for paper in fetched:
            if paper["url"] in seen_urls:
                print(f"[INFO]   SKIP (duplicate): {paper['title'][:80]}")
                continue
            if blacklist:
                haystack = (paper["title"] + " " + paper["abstract"]).lower()
                matched = next((t for t in blacklist if t in haystack), None)
                if matched:
                    print(f"[INFO]   SKIP (blacklist '{matched}'): {paper['title'][:80]}")
                    continue
            seen_urls.add(paper["url"])
            paper["fetched_date"] = fetched_date
            new_papers.append(paper)
            print(f"[INFO]   NEW: {paper['title'][:80]}")

        time.sleep(INTER_KEYWORD_SLEEP)

    print(f"[INFO] {len(new_papers)} new papers this run.")

    # --- Industry feed ingestion ---
    existing_industry = load_industry_items()
    existing_industry_urls = {item["url"] for item in existing_industry}
    feed_state = load_feed_state()
    new_industry: list = []

    if feeds_config:
        print(f"[INFO] Fetching {len(feeds_config)} industry feed(s)")
        new_industry, updated_state = fetch_industry_feeds(
            feeds_config, fetched_date, feed_state, existing_industry_urls
        )
        print(f"[INFO] {len(new_industry)} new industry item(s) this run.")
    else:
        updated_state = feed_state

    # --- Startup feed ingestion ---
    existing_startup = load_industry_items(STARTUP_JSON)
    existing_startup_urls = {item["url"] for item in existing_startup}
    startup_state = load_feed_state(STARTUP_STATE)
    new_startup: list = []

    if startup_config:
        print(f"[INFO] Fetching {len(startup_config)} startup feed(s)")
        new_startup, updated_startup_state = fetch_industry_feeds(
            startup_config, fetched_date, startup_state, existing_startup_urls, source="startup"
        )
        # Gemini-summarize new startup items
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key and new_startup:
            print(f"[INFO] Summarizing {len(new_startup)} startup item(s) with Gemini")
            for item in new_startup:
                if item.get("abstract"):
                    item["abstract"] = summarize_with_gemini(item["abstract"])
                    time.sleep(1)
        print(f"[INFO] {len(new_startup)} new startup item(s) this run.")
    else:
        updated_startup_state = startup_state

    if args.dry_run:
        print("[INFO] --dry-run: no files written.")
        return

    # --- Persist arXiv papers ---
    all_papers = existing_papers + new_papers
    save_papers(all_papers)
    print(f"[INFO] Saved {len(all_papers)} papers to {PAPERS_JSON}")

    # --- Persist industry items + feed state ---
    if feeds_config:
        all_industry = existing_industry + new_industry
        save_industry_items(all_industry)
        save_feed_state(updated_state)
        print(f"[INFO] Saved {len(all_industry)} industry items to {INDUSTRY_JSON}")
    else:
        all_industry = existing_industry

    # --- Persist startup items + feed state ---
    if startup_config:
        all_startup = existing_startup + new_startup
        save_industry_items(all_startup, STARTUP_JSON)
        save_feed_state(updated_startup_state, STARTUP_STATE)
        print(f"[INFO] Saved {len(all_startup)} startup items to {STARTUP_JSON}")
    else:
        all_startup = existing_startup

    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(generate_html(all_papers, all_industry, all_startup))

    with open(FEED_XML, "w", encoding="utf-8") as f:
        f.write(generate_rss(all_papers, site_url))  # arXiv-only, unchanged

    print(f"[INFO] Site regenerated: {len(all_papers)} arXiv + {len(all_industry)} industry + {len(all_startup)} startup items.")


if __name__ == "__main__":
    main()
