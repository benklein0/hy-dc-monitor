"""
HY Datacenter News Monitor
---------------------------
Runs on a schedule (hourly via Railway cron). For each tracked bond /
issuing entity, pulls:

  1. Corporate-level news  (parent company name)
  2. Local/site-level news (the specific city/county where the data
     center collateral sits) — this is where permitting, zoning,
     tax abatement, substation/power, and water-use stories tend to
     break first, well before they hit national coverage.

Both are pulled from Google News RSS (free, no API key), deduped against
a persisted state file, and emailed as a single digest via Gmail SMTP.
"""

import calendar
import hashlib
import json
import os
import re
import socket
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser
import requests

# Prevent a single slow/unresponsive host from hanging the entire run —
# feedparser doesn't set a network timeout by default.
socket.setdefaulttimeout(15)

# ---------------------------------------------------------------------------
# Bond / issuing-entity map.
#   name      = full issuing entity name (from the offering)
#   parent    = sponsor / parent company (used for corporate-news search)
#   locations = site location(s) tied to the collateral (used for
#               local/county news search). Leave empty list if the
#               collateral is too diffuse to search geographically
#               (e.g. CoreWeave's 41 sites).
# ---------------------------------------------------------------------------
BONDS = {
    "APLD": {
        "name": "Applied Digital - APLD ComputeCo LLC",
        "parent": "Applied Digital",
        "locations": ["Ellendale, North Dakota"],
        "coupon_maturity": "9.25% Sec. Notes due 12/15/30",
        "tenant": "CoreWeave (META guarantee)",
        "lease": "Modified Gross, 15yr",
    },
    "PFORGE": {
        "name": "Applied Digital - APLD ComputeCo 2 LLC",
        "parent": "Applied Digital",
        "locations": ["Harwood, North Dakota"],
        "coupon_maturity": "6.75% Sec. Notes due 03/15/31",
        "tenant": "Oracle",
        "lease": "Double Net, 15yr/25yr w/ options",
    },
    "ELNFOR": {
        "name": "Applied Digital - APLD ComputeCo 3 LLC",
        "parent": "Applied Digital",
        "locations": ["Ellendale, North Dakota"],
        "coupon_maturity": "7.00% Sec. Notes due 06/15/31",
        "tenant": "CoreWeave (META guarantee)",
        "lease": "Double Net, 15yr/30yr w/ options",
    },
    "CIFR": {
        "name": "Cipher Digital - Cipher Compute LLC",
        "parent": "Cipher Mining",
        "locations": ["Colorado City, Texas"],
        "coupon_maturity": "7.125% Sec. Notes due 11/15/30",
        "tenant": "Fluidstack (Google guarantee)",
        "lease": "Double Net, 10yr",
    },
    "BLKPRL": {
        "name": "Cipher Digital - Black Pearl ComputeCo LLC",
        "parent": "Cipher Mining",
        "locations": ["Wink, Texas"],
        "coupon_maturity": "6.125% Sec. Notes due 02/15/31",
        "tenant": "AWS/Amazon",
        "lease": "Triple Net, 15yr/30yr w/ options",
    },
    "STNGRY": {
        "name": "Cipher Digital - Stingray ComputCo LLC",
        "parent": "Cipher Mining",
        "locations": ["Andrews, Texas"],
        "coupon_maturity": "6.00% Sec. Notes due 06/15/31",
        "tenant": "AWS/Amazon",
        "lease": "Triple Net, 15yr/30yr w/ options; no backup generators at AWS's request",
    },
    "CORZ": {
        "name": "Core Scientific Inc (Core Scientific Finance LLC)",
        "parent": "Core Scientific",
        "locations": [
            "Denton, Texas",
            "Dalton, Georgia",
            "Muskogee, Oklahoma",
            "Marble, North Carolina",
            "Austin, Texas",
        ],
        "coupon_maturity": "7.75% Sec. Notes due 05/15/31",
        "tenant": "CoreWeave",
        "lease": "Modified Gross, 12yr w/ two 5yr options",
    },
    "CRWV": {
        "name": "CoreWeave Inc",
        "parent": "CoreWeave",
        "locations": [],  # 41 datacenters — too diffuse to search geographically
        "coupon_maturity": "9.00% Sen. Unsec. Notes due 02/01/31",
        "tenant": "MSFT, OpenAI, META, GOOGL",
        "lease": "N/A (corporate unsecured, not a single-site lease structure)",
    },
    "EDGCOM": {
        "name": "Edged Compute LLC",
        "parent": "Edged Compute",
        "locations": ["Atlanta, Georgia", "Chicago, Illinois"],
        "coupon_maturity": "7.50% Sec. Notes due 04/30/31",
        "tenant": "Alibaba (Atlanta site), CoreWeave (Chicago site)",
        "lease": "Atlanta: Modified Gross 15yr; Chicago: Triple Net 16yr",
    },
    "GALAXY": {
        "name": "Galaxy Helios Data Centers II LLC",
        "parent": "Galaxy Digital",
        "locations": ["Dickens County, Texas"],
        "coupon_maturity": "9.875% Sec. Notes due 08/01/31",
        "tenant": "CoreWeave",
        "lease": "Double Net, 15yr w/ two 5yr options",
    },
    "MERIDI": {
        "name": "Next Frontier/Fluidstack JV - Meridian Arc Holdco LLC",
        "parent": "Next Frontier / Fluidstack JV",
        "locations": ["New Lebanon, Sullivan County, Indiana"],
        "coupon_maturity": "6.25% Sec. Notes due 04/30/31",
        "tenant": "Fluidstack (Google guarantee)",
        "lease": "Triple Net, 15yr/30yr w/ options",
    },
    "ELKGVP": {
        "name": "Prime Data Centers, LLC - Elk Grove Village Property LLC",
        "parent": "Prime Data Centers",
        "locations": ["Elk Grove Village, Illinois"],
        "coupon_maturity": "7.50% Sec. Notes due 06/15/31",
        "tenant": "CoreWeave",
        "lease": "Triple Net, 15yr/29yr w/ options",
    },
    "SECMOS": {
        "name": "SB Energy - SE Cosmos, LLC",
        "parent": "SB Energy",
        "locations": ["Austin, Texas"],
        "coupon_maturity": "8.875% Sec. Notes due 05/01/31",
        "tenant": "SoftBank (OpenAI ultimately)",
        "lease": "Triple Net, 15yr/25yr w/ options",
    },
    "TRACTC": {
        "name": "Tract Capital/Fleet Data Centers - SV RNO Property Owner 1, LLC",
        "parent": "Tract / Fleet Data Centers",
        "locations": ["Storey County, Nevada"],
        "coupon_maturity": "5.875% Sec. Notes due 03/01/31",
        "tenant": "Nvidia",
        "lease": "Triple Net, 16yr/36yr w/ options",
    },
    "TRACTD": {
        "name": "Tract Capital/Fleet Data Centers - PR RNO Property Owner 1, LLC",
        "parent": "Tract / Fleet Data Centers",
        "locations": ["Storey County, Nevada"],
        "coupon_maturity": "6.50% Sec. Notes due 05/01/31",
        "tenant": "Nvidia",
        "lease": "Triple Net, 16yr/36yr w/ options",
    },
    "WULF": {
        "name": "TeraWulf - WULF Compute LLC",
        "parent": "TeraWulf",
        "locations": ["Barker, New York"],
        "coupon_maturity": "7.75% Sec. Notes due 10/15/30",
        "tenant": "La Lupa campus: Core42/G42 (Abu Dhabi guarantee); Akela campus: Fluidstack (Google guarantee)",
        "lease": "Modified Gross, 10yr (both campuses)",
    },
    "FLASHC": {
        "name": "TeraWulf/Fluidstack JV - Flash Compute LLC",
        "parent": "TeraWulf",
        "locations": ["Abernathy, Texas"],
        "coupon_maturity": "7.25% Sec. Notes due 12/31/30",
        "tenant": "Fluidstack (Google guarantee)",
        "lease": "Modified Gross, 15yr/25yr w/ options",
    },
    "YNDRDC": {
        "name": "Yondr Group - Yondr JK 1, LLC",
        "parent": "Yondr Group",
        "locations": ["Loudoun County, Virginia"],
        "coupon_maturity": "6.875% Sec. Notes due 06/30/31",
        "tenant": "Oracle",
        "lease": "Modified Gross, 15yr/25yr w/ options",
    },
    # No bond ticker known yet — "ZENARC" is a placeholder key I made up
    # so this fits the existing dict structure. Swap it for the real
    # ticker once it's assigned; nothing else needs to change.
    "ZENARC": {
        "name": "Zenith Arc LLC",
        "parent": "Zenith Arc",
        "locations": ["Okmulgee, Oklahoma"],
        "coupon_maturity": "unknown — no offering details yet",
        "tenant": "unconfirmed",
        "lease": "unknown",
    },
}

# Some parent labels above are my own descriptive shorthand, not names that
# actually appear in news articles (e.g. nobody writes "Tract / Fleet Data
# Centers" verbatim). For those, override the corporate search query with
# terms that actually match real coverage.
CORPORATE_SEARCH_OVERRIDES = {
    "Tract / Fleet Data Centers": '("Tract Capital" OR (Tract AND (data center OR Nevada OR Storey)))',
    "Next Frontier / Fluidstack JV": '("Next Frontier Data Centers" OR Fluidstack)',
}

# Site-specific proper nouns (utility companies, project/campus nicknames,
# county names) researched per site — these are searched directly since a
# lot of trade-press and local coverage uses the utility or project name
# rather than the town name (e.g. TeraWulf's Barker, NY site is publicly
# known as "Lake Mariner", in the Town of Somerset).
SITE_KEYWORDS = {
    "Ellendale, North Dakota": ["Montana-Dakota Utilities", "MDU", "Dickey County", "Polaris Forge"],
    "Harwood, North Dakota": ["Cass County Electric Cooperative", "Polaris Forge 2", "Minnkota Power"],
    "Colorado City, Texas": ["Barber Lake", "Mitchell County"],
    "Wink, Texas": ["Black Pearl", "Winkler County", "Kermit Texas"],
    "Andrews, Texas": ["Stingray", "Andrews County", "Lyntegar Electric"],
    "Denton, Texas": ["Denton Municipal Electric"],
    "Dalton, Georgia": ["Dalton Utilities", "Whitfield County"],
    "Muskogee, Oklahoma": ["Port of Muskogee", "Muskogee City-County Port Authority", "OG&E"],
    "Marble, North Carolina": ["Cherokee County North Carolina", "Duke Energy", "Blue Ridge Mountain EMC"],
    "Austin, Texas": ["Austin Energy"],
    "Atlanta, Georgia": ["Georgia Power"],
    "Chicago, Illinois": ["ComEd", "Commonwealth Edison"],
    "Dickens County, Texas": [],
    "New Lebanon, Sullivan County, Indiana": ["Hoosier Energy", "Duke Energy Indiana", "Frontier Development Holdings"],
    "Elk Grove Village, Illinois": ["ComEd", "Commonwealth Edison", "Cook County"],
    "Storey County, Nevada": ["Tahoe Reno Industrial Center", "TRIC", "NV Energy"],
    "Barker, New York": ["Lake Mariner", "Somerset New York", "Niagara County", "National Grid"],
    "Abernathy, Texas": ["Hale County Texas", "Lubbock County"],
    "Loudoun County, Virginia": ["Dominion Energy", "Data Center Alley"],
    "Okmulgee, Oklahoma": [
        "Redd Ridge Consulting", "Three Rivers Manufacturing", "Hodges Warehouse",
        "Okmulgee Area Development Corporation", "East Central Oklahoma Electric",
    ],
}

# Additional curated feeds beyond Nevada Independent, mostly nonprofit
# statehouse/regional outlets (States Newsroom network + similar) that
# cover energy/utility and data-center-development stories closely.
# NOTE: these feed URLs are researched but not all individually verified —
# if one 404s, drop it or find the correct feed path for that outlet.
# Curated direct RSS feeds for outlets known to cover a specific site closely
# — a higher-reliability supplement to Google News search, since smaller
# regional/state outlets can be slow to surface or rank low in search results.
# match_terms: entries are kept only if title/summary contain at least one
# (case-insensitive substring match) — feeds are site-wide, not topic-filtered.
CURATED_FEEDS = {
    "Storey County, Nevada": [
        {
            "feed_url": "https://thenevadaindependent.com/feed/",
            "match_terms": [
                "data center", "datacenter", "tract", "storey county",
                "tric", "nv energy", "tahoe reno",
            ],
        },
    ],
}

CURATED_FEEDS.update({
    "Ellendale, North Dakota": [
        {"feed_url": "https://northdakotamonitor.com/feed/",
         "match_terms": ["data center", "applied digital", "ellendale", "mdu", "montana-dakota"]},
    ],
    "Harwood, North Dakota": [
        {"feed_url": "https://northdakotamonitor.com/feed/",
         "match_terms": ["data center", "applied digital", "harwood", "cass county electric", "polaris forge"]},
    ],
    "Dalton, Georgia": [
        {"feed_url": "https://georgiarecorder.com/feed/",
         "match_terms": ["data center", "dalton", "core scientific", "dalton utilities", "whitfield county"]},
    ],
    "Atlanta, Georgia": [
        {"feed_url": "https://georgiarecorder.com/feed/",
         "match_terms": ["data center", "georgia power", "edged compute"]},
    ],
    "Muskogee, Oklahoma": [
        {"feed_url": "https://oklahomavoice.com/feed/",
         "match_terms": ["data center", "muskogee", "core scientific", "port of muskogee"]},
    ],
    "Okmulgee, Oklahoma": [
        {"feed_url": "https://oklahomavoice.com/feed/",
         "match_terms": ["data center", "okmulgee", "zenith arc", "redd ridge"]},
    ],
    "Marble, North Carolina": [
        {"feed_url": "https://ncnewsline.com/feed/",
         "match_terms": ["data center", "marble", "cherokee county", "core scientific"]},
    ],
    "New Lebanon, Sullivan County, Indiana": [
        {"feed_url": "https://indianacapitalchronicle.com/feed/",
         "match_terms": ["data center", "sullivan county", "fluidstack", "next frontier"]},
    ],
    "Loudoun County, Virginia": [
        {"feed_url": "https://virginiamercury.com/feed/",
         "match_terms": ["data center", "loudoun", "yondr", "dominion energy"]},
    ],
    "Elk Grove Village, Illinois": [
        {"feed_url": "https://capitolnewsillinois.com/feed/",
         "match_terms": ["data center", "elk grove village", "prime data centers", "comed"]},
    ],
    "Chicago, Illinois": [
        {"feed_url": "https://capitolnewsillinois.com/feed/",
         "match_terms": ["data center", "chicago", "edged compute", "comed"]},
    ],
})

SEEN_FILE = os.environ.get("SEEN_FILE_PATH", "seen_articles.json")

# Local-news queries are narrowed with these keywords so a location like
# "Austin, Texas" doesn't pull in unrelated city news. Covers permitting/
# zoning, tax/fiscal, utility & power infrastructure, and litigation/
# regulatory disputes — the categories that tend to move HY credit views
# on site-specific project finance debt.
BASE_LOCAL_TERMS = [
    "data center", "datacenter", "rezoning", "zoning", "tax abatement",
    "substation", "power purchase", "moratorium", "county commissioners",
    "planning commission", "water use", "permit", "lawsuit", "sues",
    "sued", "litigation", "dispute", "utility", "electricity",
    "power plant", "grid", "interconnection", "public utilities commission",
    "regulator", "regulators",
]

# Tenant/hyperscaler + grid interconnect (ISO/RTO) terms per location,
# pulled from the lease/tenant detail table. These co-occur with the
# location name in the query (unlike SITE_KEYWORDS below, which are
# distinctive enough to search standalone). Tenant names alone (e.g.
# "Oracle", "Google") are too generic to search without a location anchor.
TENANT_KEYWORDS = {
    "Ellendale, North Dakota": ["CoreWeave", "SPP interconnect"],
    "Harwood, North Dakota": ["Oracle", "SPP interconnect"],
    "Colorado City, Texas": ["Fluidstack", "ERCOT"],
    "Wink, Texas": ["AWS", "Amazon Web Services", "ERCOT"],
    "Andrews, Texas": ["AWS", "Amazon Web Services", "ERCOT"],
    "Denton, Texas": ["CoreWeave"],
    "Dalton, Georgia": ["CoreWeave"],
    "Muskogee, Oklahoma": ["CoreWeave"],
    "Marble, North Carolina": ["CoreWeave"],
    "Austin, Texas": ["CoreWeave", "SoftBank", "OpenAI", "ERCOT"],
    "Atlanta, Georgia": ["Alibaba"],
    "Chicago, Illinois": ["CoreWeave", "PJM"],
    "Dickens County, Texas": ["CoreWeave", "ERCOT"],
    "New Lebanon, Sullivan County, Indiana": ["Fluidstack", "Google", "MISO interconnect"],
    "Elk Grove Village, Illinois": ["CoreWeave", "PJM interconnect"],
    "Storey County, Nevada": ["Nvidia"],
    "Barker, New York": ["Core42", "G42", "Fluidstack", "NYISO"],
    "Abernathy, Texas": ["Fluidstack", "Google", "ERCOT"],
    "Loudoun County, Virginia": ["Oracle", "PJM Dominion"],
    "Okmulgee, Oklahoma": ["Fluidstack", "Public Service Company of Oklahoma", "SPP"],
}


def _keyword_clause(terms):
    return "(" + " OR ".join(f'"{t}"' if " " in t else t for t in terms) + ")"

LOOKBACK_WINDOW = "1d"          # Google News RSS "when:" operator
MAX_SEEN_AGE_DAYS = 14
REQUEST_DELAY_SECONDS = 1.5

RESEND_API_KEY = "".join(os.environ["RESEND_API_KEY"].split())
EMAIL_FROM = "".join(os.environ["EMAIL_FROM"].split())
ANTHROPIC_API_KEY = "".join(os.environ["ANTHROPIC_API_KEY"].split())
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()


def _check_ascii(name, value):
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise SystemExit(
            f"\n[config error] {name} contains a non-ASCII character (often caused by "
            f"curly/smart quotes sneaking in when an export command is pasted from Notes, "
            f"Word, etc. instead of typed directly). Re-export it using plain straight "
            f'quotes, e.g.:\n  export {name}="your_value_here"\n'
        )


_check_ascii("RESEND_API_KEY", RESEND_API_KEY)
_check_ascii("EMAIL_FROM", EMAIL_FROM)
_check_ascii("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)

# Comma-separated list of recipients, e.g. "a@example.com,b@example.com"
ALERT_EMAIL_TO = [
    addr.strip()
    for addr in os.environ.get("ALERT_EMAIL_TO", EMAIL_FROM).split(",")
    if addr.strip()
]


# ---------------------------------------------------------------------------
# Derived lookup tables: unique parents / locations -> tickers referencing them
# ---------------------------------------------------------------------------
def build_query_groups():
    parents = defaultdict(list)   # parent name -> [tickers]
    locations = defaultdict(list)  # location string -> [tickers]
    for ticker, info in BONDS.items():
        parents[info["parent"]].append(ticker)
        for loc in info["locations"]:
            locations[loc].append(ticker)
    return parents, locations


PARENT_GROUPS, LOCATION_GROUPS = build_query_groups()


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_seen(seen):
    parent_dir = os.path.dirname(SEEN_FILE)
    if parent_dir and not os.path.isdir(parent_dir):
        try:
            os.makedirs(parent_dir, exist_ok=True)
        except OSError as e:
            print(f"[warn] could not create directory for SEEN_FILE_PATH ({parent_dir}): {e}")
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def trim_seen(seen):
    cutoff = time.time() - MAX_SEEN_AGE_DAYS * 86400
    return {k: v for k, v in seen.items() if v.get("first_seen", 0) > cutoff}


def article_key(entry):
    basis = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# Hard cutoff: nothing older than this ever makes it into an alert, no
# matter the source. Google News's "when:1d" is a best-effort filter, not
# a guarantee, and curated/site-specific RSS feeds have no date filtering
# at all — so this is the actual enforcement point.
MAX_ARTICLE_AGE_DAYS = 14


def _is_recent_enough(entry, max_age_days=MAX_ARTICLE_AGE_DAYS):
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return True  # no date info available — don't drop, can't confirm either way
    published_ts = calendar.timegm(struct)
    age_days = (time.time() - published_ts) / 86400
    return age_days <= max_age_days


# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------
def fetch_news(query):
    encoded = urllib.parse.quote(f"{query} when:{LOOKBACK_WINDOW}")
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    return feed.entries


def fetch_corporate_news(parent_name):
    query = CORPORATE_SEARCH_OVERRIDES.get(parent_name, f'"{parent_name}"')
    return fetch_news(query)


def fetch_site_specific_news(location):
    """Search researched proper nouns (utility names, project nicknames,
    county names). These co-occur with a data-center/energy anchor term
    (not the town name itself) rather than being searched fully standalone
    — several of these terms (utility companies, county names) are common
    enough on their own to pull in unrelated local news (e.g. "Whitfield
    County" alone matches routine high-school sports coverage)."""
    terms = SITE_KEYWORDS.get(location, [])
    if not terms:
        return []
    anchor = _keyword_clause(BASE_LOCAL_TERMS + ["compute", "hyperscale"])
    query = f"{_keyword_clause(terms)} {anchor}"
    return fetch_news(query)


# Terms that alone just mean "this article is about the general topic
# area" — curated feed matches require one of these PLUS at least one
# more specific identifying term, so a bare county/city name isn't
# sufficient on its own (avoids false positives like local sports
# coverage that happens to mention the county name).
_CURATED_ANCHOR_TERMS = ["data center", "datacenter", "compute", "hyperscale"]


def fetch_curated_entries(location):
    """Poll any curated direct RSS feeds registered for this location and
    return only entries matching an anchor term plus a specific identifier."""
    results = []
    for source in CURATED_FEEDS.get(location, []):
        try:
            feed = feedparser.parse(source["feed_url"])
        except Exception as e:
            print(f"[warn] curated feed fetch failed for {source['feed_url']}: {e}")
            continue
        specific_terms = [
            t.lower() for t in source["match_terms"] if t.lower() not in _CURATED_ANCHOR_TERMS
        ]
        for entry in feed.entries:
            haystack = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            has_anchor = any(a in haystack for a in _CURATED_ANCHOR_TERMS)
            has_specific = any(t in haystack for t in specific_terms) if specific_terms else True
            if has_anchor and has_specific:
                results.append(entry)
    return results


def fetch_local_news(location):
    terms = BASE_LOCAL_TERMS + TENANT_KEYWORDS.get(location, [])
    query = f'"{location}" {_keyword_clause(terms)}'
    return fetch_news(query)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _render_items_html(items):
    lis = []
    for item, analysis in items:
        title = item.get("title", "Untitled")
        link = item.get("link", "")
        published = item.get("published", "")
        source = item.get("source", {}).get("title", "") if hasattr(item, "get") else ""
        analysis_html = f'<br><em>Impact: {analysis}</em>' if analysis else ""
        lis.append(
            f'<li><a href="{link}">{title}</a><br><small>{source} — {published}</small>'
            f'{analysis_html}</li>'
        )
    return "<ul>" + "".join(lis) + "</ul>"


def _render_items_text(items):
    lines = []
    for item, analysis in items:
        lines.append(f"  - {item.get('title', 'Untitled')} ({item.get('link', '')})")
        if analysis:
            lines.append(f"    Impact: {analysis}")
    return "\n".join(lines)


def build_email(new_corporate, new_local):
    total = sum(len(v) for v in new_corporate.values()) + sum(len(v) for v in new_local.values())
    subject = f"HY Datacenter News Alert — {total} new item{'s' if total != 1 else ''}"

    html_parts = [f"<p>New, market-relevant news for tracked HY datacenter bonds (last {LOOKBACK_WINDOW}):</p>"]
    text_parts = ["New, market-relevant news for tracked HY datacenter bonds:"]

    if new_corporate:
        html_parts.append("<h2>Corporate News</h2>")
        text_parts.append("\n=== CORPORATE NEWS ===")
        for parent, items in new_corporate.items():
            tickers = ", ".join(PARENT_GROUPS[parent])
            html_parts.append(f"<h3>{parent} ({tickers})</h3>")
            html_parts.append(_render_items_html(items))
            text_parts.append(f"\n{parent} ({tickers})")
            text_parts.append(_render_items_text(items))

    if new_local:
        html_parts.append("<h2>Local / Site News</h2>")
        text_parts.append("\n=== LOCAL / SITE NEWS ===")
        for location, items in new_local.items():
            tickers = ", ".join(LOCATION_GROUPS[location])
            html_parts.append(f"<h3>{location} ({tickers})</h3>")
            html_parts.append(_render_items_html(items))
            text_parts.append(f"\n{location} ({tickers})")
            text_parts.append(_render_items_text(items))

    html = f"<html><body>{''.join(html_parts)}</body></html>"
    text = "\n".join(text_parts)

    return {"subject": subject, "html": html, "text": text}


def send_email(msg):
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": ALERT_EMAIL_TO,
            "subject": msg["subject"],
            "html": msg["html"],
            "text": msg["text"],
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TITLE_SIMILARITY_THRESHOLD = 0.85  # 0-1; higher = stricter match required


def _normalize_title(title):
    t = title.strip()
    # Google News (and some outlets) append " - Source Name" to titles;
    # strip that so the same story from two sources compares cleanly.
    if " - " in t:
        t = t.rsplit(" - ", 1)[0]
    t = re.sub(r"\W+", " ", t.lower()).strip()
    return t


def _is_similar_to_any(norm_title, seen_titles, threshold=TITLE_SIMILARITY_THRESHOLD):
    if not norm_title:
        return False
    for existing in seen_titles:
        if SequenceMatcher(None, norm_title, existing).ratio() >= threshold:
            return True
    return False


def _dedupe_fresh(entries, seen, seen_titles, tag):
    fresh = []
    for entry in entries:
        if not _is_recent_enough(entry):
            continue
        key = article_key(entry)
        if key in seen:
            continue
        title = entry.get("title", "")
        norm = _normalize_title(title)
        is_title_dup = _is_similar_to_any(norm, seen_titles)
        seen[key] = {"first_seen": time.time(), "tag": tag, "title": title}
        if is_title_dup:
            continue  # same story as something already alerted on/seen this run
        if norm:
            seen_titles.add(norm)
        fresh.append(entry)
    return fresh


# ---------------------------------------------------------------------------
# Relevance + impact analysis (Claude)
# ---------------------------------------------------------------------------
# The keyword search above is intentionally broad — it's a recall tool, not
# a precision tool. A "lawsuit" keyword match can just as easily hit a
# generic legal-blog explainer about crypto disclosure law in Ontario as it
# can hit an actual lawsuit against one of our tenants. This step adds real
# judgment on top: for each batch of keyword-matched candidates, ask Claude
# whether the article is (a) actually about this specific issuer/site/tenant,
# not just a coincidental keyword overlap, and (b) plausibly market-moving /
# credit-relevant for that bond. Only items that pass both checks make it
# into the email, each with a short bond-specific impact note attached.
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

_RELEVANCE_SYSTEM_PROMPT = """You are a high-yield credit analyst screening news for a datacenter bond monitoring system.

You will be given a bond/issuer group (ticker(s) with coupon/maturity, tenant, and lease structure; issuer name; site location; whether this is a LOCAL/SITE-level search or a CORPORATE-level search) and a list of candidate news articles that matched a keyword search. The keyword search is deliberately broad and produces a lot of noise — coincidental keyword overlaps, generic legal-explainer content, unrelated companies, routine local news with no real connection to this issuer or site, and derivative commentary that isn't actual new reporting.

For EACH article, decide:
1. Is it actually about this specific issuer, its tenant, or this specific site/location — not just a coincidental keyword match?
2. Is it plausibly market-moving or credit-relevant for this bond?
3. Is it primary, incremental reporting — an actual new fact or development — rather than derivative commentary or a rehash? Reject anything whose content is fundamentally reactive/secondary rather than a new underlying fact.

On weighting LOCAL vs. CORPORATE news (criterion 2): this system exists specifically because for site-specific HY project-finance datacenter debt, LOCAL/SITE-level news — permitting and zoning votes or reversals, county/planning commission decisions, utility and interconnection disputes or delays, tax abatement votes, litigation tied to the specific site, water/power use disputes, organized local opposition that could affect timeline — is very often the single most important, earliest credit signal, even when it doesn't resemble traditional "market-moving" corporate news. When the search context is LOCAL/SITE-level, apply a MODERATE bar: genuine, confirmed site-specific governmental/regulatory/utility/litigation developments should be marked relevant even if their scale seems modest, because early local signals on permitting or utility disputes are exactly what this feed is for. When the search context is CORPORATE-level, apply a HIGHER bar: require a clear, specific tie to this bond's actual economics (the tenant's ability to pay under the lease, the issuer's financing, ratings, litigation) — general company profiles, valuation milestones, or PR without a specific credit-relevant mechanism should usually be marked not relevant unless the connection to this bond's cash flows or collateral is direct and explained.

Mark NOT relevant regardless of context:
- Generic explainer/legal-blog content not about this issuer
- Unrelated companies/entities that happen to share a keyword
- Routine local news (sports, weather, general community events) with no substantive connection to this bond
- Stock technical-analysis or price-action commentary ("why X stock moved today", chart/momentum/analyst-rating pieces, "3 stocks to watch" listicles) — these react to price action, they are not news about the issuer itself
- Opinion, recap, or "explainer" pieces that just restate or synthesize previously reported facts without any new development
- Aggregator/wire rehashes of a story with no new information beyond what a prior article already covered
- Valuation milestones, funding-round announcements, or general "milestone reached" PR about a tenant/parent that state a headline number (e.g. "Company X hits $18B valuation") WITHOUT a specific, stated mechanism connecting it to this bond's cash flows, collateral, or counterparty risk. A valuation figure alone does not tell you whether lease terms, tenant ability-to-pay, or site-level risk changed — it would not itself move trading in this bond. Only mark such items relevant if the article explicitly ties the raise/valuation to something mechanistic for this specific lease or site (e.g. stated use-of-proceeds funding this site's buildout, a covenant change, a stated liquidity commitment to this project).
- Anything you're not reasonably confident is actually about this specific issuer/site

When in doubt between "interesting but tangential" and "not relevant," or between "primary news" and "derivative commentary," mark it not relevant — this feed should be selective and only surface genuinely new, primary developments, not comprehensive coverage of everything mentioning these names.

For each relevant article, the analysis must be specific to the bond(s) given — cite the actual ticker(s), and where it strengthens the point, the coupon/maturity, tenant, or lease structure provided (e.g. "credit positive for the 6.25% MERIDI notes due 4/30/31, where Fluidstack sits under a Google-guaranteed triple-net lease — reduced counterparty risk on the lease servicing the notes"). Do not write generically about "the parent company" when specific bond terms are available and relevant — reference the actual bond.

Respond with ONLY a JSON array, no other text, no markdown code fences, one object per article in the same order given:
[{"index": 0, "relevant": true, "analysis": "1-2 sentence bond-specific impact citing the ticker and relevant bond terms"}, {"index": 1, "relevant": false, "analysis": ""}]"""


def _call_claude(system_prompt, user_prompt, max_tokens=2000):
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def _parse_json_array(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _bond_detail_lines(tickers):
    lines = []
    for t in tickers:
        info = BONDS.get(t)
        if not info:
            lines.append(f"- {t}: (no bond detail on file)")
            continue
        lines.append(
            f"- {t}: {info.get('coupon_maturity', 'n/a')} | "
            f"Tenant: {info.get('tenant', 'n/a')} | Lease: {info.get('lease', 'n/a')}"
        )
    return "\n".join(lines)


def assess_relevance(group_label, tickers, entries, context_type):
    """Filters a batch of keyword-matched candidates down to genuinely
    relevant, bond-specific, market-moving items, each paired with a short
    impact analysis. context_type is "corporate" or "local" — it changes
    how strictly the relevance bar is applied (see system prompt). Returns
    a list of (entry, analysis) tuples.

    Fails open (keeps all candidates, unanalyzed) if the API call or
    response parsing fails, rather than silently dropping everything —
    a noisy run is recoverable, a silently empty one isn't."""
    if not entries:
        return []

    article_lines = []
    for i, entry in enumerate(entries):
        title = entry.get("title", "")
        summary = (entry.get("summary", "") or "")[:300]
        article_lines.append(f"{i}. TITLE: {title}\n   SUMMARY: {summary}")

    user_prompt = (
        f"Search context: {context_type.upper()}\n"
        f"Bond group: {group_label}\n"
        f"Bond detail:\n{_bond_detail_lines(tickers)}\n\n"
        f"Candidate articles:\n" + "\n".join(article_lines)
    )

    try:
        raw = _call_claude(_RELEVANCE_SYSTEM_PROMPT, user_prompt)
        results = _parse_json_array(raw)
    except Exception as e:
        print(f"[warn] Claude relevance check failed for {group_label}: {e} "
              f"— keeping all {len(entries)} candidate(s) unfiltered as fallback")
        return [(entry, "") for entry in entries]

    kept = []
    for r in results:
        idx = r.get("index")
        if idx is None or not isinstance(idx, int) or not (0 <= idx < len(entries)):
            continue
        if r.get("relevant"):
            kept.append((entries[idx], r.get("analysis", "")))
    return kept


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting run: "
          f"{len(PARENT_GROUPS)} corporate + {len(LOCATION_GROUPS)} local queries "
          f"(plus site-specific + curated feeds per location)")
    seen = load_seen()
    seen_titles = {
        _normalize_title(v["title"])
        for v in seen.values()
        if v.get("title") and _normalize_title(v["title"])
    }
    new_corporate = {}
    new_local = {}

    for i, parent in enumerate(PARENT_GROUPS, 1):
        print(f"  [{i}/{len(PARENT_GROUPS)}] corporate: {parent}")
        try:
            entries = fetch_corporate_news(parent)
        except Exception as e:
            print(f"[warn] corporate fetch failed for {parent}: {e}")
            entries = []
        fresh = _dedupe_fresh(entries, seen, seen_titles, f"corp:{parent}")
        if fresh:
            new_corporate[parent] = fresh
        time.sleep(REQUEST_DELAY_SECONDS)

    for i, location in enumerate(LOCATION_GROUPS, 1):
        print(f"  [{i}/{len(LOCATION_GROUPS)}] local: {location}")
        try:
            entries = fetch_local_news(location)
        except Exception as e:
            print(f"[warn] local fetch failed for {location}: {e}")
            entries = []
        try:
            entries += fetch_site_specific_news(location)
        except Exception as e:
            print(f"[warn] site-specific fetch failed for {location}: {e}")
        entries += fetch_curated_entries(location)
        fresh = _dedupe_fresh(entries, seen, seen_titles, f"local:{location}")
        if fresh:
            new_local[location] = fresh
        time.sleep(REQUEST_DELAY_SECONDS)

    seen = trim_seen(seen)
    save_seen(seen)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Assessing relevance with Claude...")
    for parent in list(new_corporate.keys()):
        entries = new_corporate[parent]
        print(f"  assessing corporate: {parent} ({len(entries)} candidate(s))")
        kept = assess_relevance(parent, PARENT_GROUPS[parent], entries, context_type="corporate")
        if kept:
            new_corporate[parent] = kept
        else:
            del new_corporate[parent]

    for location in list(new_local.keys()):
        entries = new_local[location]
        print(f"  assessing local: {location} ({len(entries)} candidate(s))")
        kept = assess_relevance(location, LOCATION_GROUPS[location], entries, context_type="local")
        if kept:
            new_local[location] = kept
        else:
            del new_local[location]

    if new_corporate or new_local:
        msg = build_email(new_corporate, new_local)
        send_email(msg)
        total = sum(len(v) for v in new_corporate.values()) + sum(len(v) for v in new_local.values())
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sent alert with {total} new item(s).")
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No new items this run.")


if __name__ == "__main__":
    main()
