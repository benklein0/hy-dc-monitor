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

# Optional — enables the cross-model disagreement report (see
# cross_model_disagreement_report below). If either is unset, that
# provider is simply skipped; the core pipeline is unaffected either way.
XAI_API_KEY = "".join(os.environ.get("XAI_API_KEY", "").split())
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast").strip()
OPENAI_API_KEY = "".join(os.environ.get("OPENAI_API_KEY", "").split())
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()


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
if XAI_API_KEY:
    _check_ascii("XAI_API_KEY", XAI_API_KEY)
if OPENAI_API_KEY:
    _check_ascii("OPENAI_API_KEY", OPENAI_API_KEY)

# Comma-separated list of recipients, e.g. "a@example.com,b@example.com"
ALERT_EMAIL_TO = [
    addr.strip()
    for addr in os.environ.get("ALERT_EMAIL_TO", EMAIL_FROM).split(",")
    if addr.strip()
]


# Recipients for the broader QC/review digest (on-topic items excluded from
# the strict alert, for tuning filter accuracy). Defaults to just the first
# ALERT_EMAIL_TO address rather than the full distribution — this is a
# tuning tool, not something colleagues need cluttering their inbox with.
# Set explicitly to override, e.g. to send it to nobody functionally by
# pointing it at an address you don't check, or to broaden it later.
REVIEW_EMAIL_TO = [
    addr.strip()
    for addr in os.environ.get("REVIEW_EMAIL_TO", ALERT_EMAIL_TO[0]).split(",")
    if addr.strip()
]

# Recipients for the cross-model disagreement report (see
# cross_model_disagreement_report). Defaults to REVIEW_EMAIL_TO. Only sent
# when at least one of XAI_API_KEY / OPENAI_API_KEY is configured AND at
# least one actual disagreement occurred — not sent every run.
CROSS_MODEL_REPORT_EMAIL_TO = [
    addr.strip()
    for addr in os.environ.get("CROSS_MODEL_REPORT_EMAIL_TO", ",".join(REVIEW_EMAIL_TO)).split(",")
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

# Real public equity tickers, for the subset of parents that actually trade
# publicly. NOTE: most of the "tickers" in BONDS are internal bond/SPV
# shorthand (STNGRY, PFORGE, GALAXY, etc.), not tradable stock symbols —
# searching those literally would find nothing. This map is only for
# genuinely public parents, used as an extra raw-recall search term.
# Galaxy Digital's real ticker is GLXY, not "GALAXY" (our internal bond
# code for that entry).
PUBLIC_TICKER_OVERRIDE = {
    "Applied Digital": "APLD",
    "Cipher Mining": "CIFR",
    "Core Scientific": "CORZ",
    "CoreWeave": "CRWV",
    "Galaxy Digital": "GLXY",
    "TeraWulf": "WULF",
}


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


# How far back to look when telling Claude what's already been alerted on
# for a given bond group, for semantic-duplicate detection (catching the
# same underlying story reworded by a different outlet — e.g. "SB Energy
# files for IPO" vs "American data center operator SB Energy is planning
# an IPO" — which fuzzy title-matching alone won't catch since the wording
# differs too much). Kept shorter than MAX_SEEN_AGE_DAYS (which governs how
# long we remember hashes purely for exact-dedup purposes).
RECENTLY_ALERTED_LOOKBACK_DAYS = 10


def _recent_alerted_titles(seen, tag, max_age_days=RECENTLY_ALERTED_LOOKBACK_DAYS):
    cutoff = time.time() - max_age_days * 86400
    return [
        v["title"] for v in seen.values()
        if v.get("tag") == tag and v.get("strict_relevant") and v.get("title")
        and v.get("first_seen", 0) > cutoff
    ]


def article_key(entry):
    basis = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# Hard cutoff: nothing older than this ever makes it into an alert, no
# matter the source. Google News's "when:1d" is a best-effort filter, not
# a guarantee (a Denton, TX article with an actual Aug 21 byline once
# surfaced in a Sept 3 run, 13 days later — within the old 14-day cutoff
# but obviously stale for an hourly-cadence feed), and curated/site-specific
# RSS feeds have no date filtering of their own at all — so this is the
# actual enforcement point. Kept short since this runs hourly; there's no
# reason a genuinely new story should be more than a couple days old by
# the time some layer surfaces it.
MAX_ARTICLE_AGE_DAYS = 3


def _is_recent_enough(entry, max_age_days=MAX_ARTICLE_AGE_DAYS):
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return True  # no date info available — don't drop, can't confirm either way
    published_ts = calendar.timegm(struct)
    age_days = (time.time() - published_ts) / 86400
    return age_days <= max_age_days


# Outlets that are structurally secondary/derivative — contributor-driven
# stock commentary, technical analysis, and content-mill sites rather than
# primary reporting. Blocked outright regardless of what an individual
# article says, since relying on the LLM to catch every instance of "this
# entire publication is opinion/analysis, not news" isn't reliable. Matched
# against the article's source name, NOT its link domain — Google News RSS
# wraps links through news.google.com, so the true publisher domain often
# isn't recoverable from the link itself; the source name (or the
# " - Source Name" suffix Google appends to titles) is the reliable signal.
BLOCKED_SOURCES = {
    "seeking alpha",
    "motley fool", "the motley fool",
    "zacks", "zacks investment research",
    "benzinga",
    "investorplace",
    "marketbeat",
    "simply wall st", "simplywall.st",
    "gurufocus",
    "insider monkey",
    "tipranks",
    "barchart",
    "24/7 wall st", "247wallst",
}


def _entry_source_name(entry):
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return source["title"].strip()
    title = entry.get("title", "")
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()
    return ""


def _is_blocked_source(entry):
    name = _entry_source_name(entry).lower()
    return any(blocked in name for blocked in BLOCKED_SOURCES)


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


def fetch_raw_ticker_news(parent_name):
    """Raw, ungated search on the actual public equity ticker (not our
    internal bond code) for parents that trade publicly. No anchor-term
    requirement — this exists specifically to catch real coverage gaps
    where an article doesn't happen to use any of our keyword-search
    vocabulary. Feeds the same downstream dedup + Claude assessment as
    every other layer, so a genuine miss here can still land in the main
    alert, not just the QC review digest."""
    ticker = PUBLIC_TICKER_OVERRIDE.get(parent_name)
    if not ticker:
        return []
    return fetch_news(f'"{ticker}"')


# Locations too large/generic for a bare-name search to be useful — these
# are real cities with enormous daily news volume unrelated to the site
# (Austin: Tesla/SXSW/state politics/every local human-interest story;
# Chicago: similarly enormous). A raw search here doesn't meaningfully
# improve recall (the gated fetch_local_news + fetch_site_specific_news
# layers already cover these) and risks generating a candidate batch large
# enough to overwhelm a single Claude call. This is exactly the flood
# fetch_local_news's anchor-term gating exists to prevent — skip the raw
# layer for these specifically rather than reopening that hole.
RAW_LOCATION_SEARCH_EXCLUDE = {
    "Austin, Texas",
    "Chicago, Illinois",
}


def fetch_raw_location_news(location):
    """Raw, ungated search on just the bare location name — no anchor-term
    or keyword requirement. Broader and noisier than fetch_local_news, by
    design: this is the recall backstop for the case where a genuinely
    relevant site-level article doesn't use any of our anchor vocabulary
    (zoning, utility, lawsuit, etc.). The extra noise this produces is
    handled downstream by the Claude on_topic/market_moving check, same
    as every other layer — except for RAW_LOCATION_SEARCH_EXCLUDE, where
    the location name alone is too generic/high-volume for this to be
    safe at any batch size."""
    if location in RAW_LOCATION_SEARCH_EXCLUDE:
        return []
    return fetch_news(f'"{location}"')


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


def build_email(new_corporate, new_local, subject_prefix="HY Datacenter News Alert",
                 intro="New, market-relevant news for tracked HY datacenter bonds"):
    total = sum(len(v) for v in new_corporate.values()) + sum(len(v) for v in new_local.values())
    subject = f"{subject_prefix} — {total} new item{'s' if total != 1 else ''}"

    html_parts = [f"<p>{intro} (last {LOOKBACK_WINDOW}):</p>"]
    text_parts = [f"{intro}:"]

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


def build_disagreement_email(records):
    """Builds the cross-model disagreement report — a flat list of
    articles where Grok and/or GPT's strict_relevant call differed from
    Claude's, each showing every model's verdict and reasoning side by
    side. Purely diagnostic; never sent unless there's at least one
    actual disagreement."""
    subject = f"HY Datacenter News — Cross-Model Disagreement Report — {len(records)} item{'s' if len(records) != 1 else ''}"

    html_parts = [
        "<p>Articles where Grok and/or GPT's relevance call differed from Claude's "
        "(Claude's call is what actually gates the main alert — this is a diagnostic "
        "comparison, not a second opinion that changes anything automatically):</p>"
    ]
    text_parts = ["Articles where another model's relevance call differed from Claude's:"]

    for r in records:
        title = r["entry"].get("title", "Untitled")
        link = r["entry"].get("link", "")
        group = r["group_label"]
        claude_v = r["claude"]

        html_parts.append(f'<h3><a href="{link}">{title}</a></h3>')
        html_parts.append(f"<p><em>{group}</em></p><ul>")
        html_parts.append(
            f"<li><b>Claude</b> — relevant: {claude_v['strict_relevant']} — {claude_v['analysis']}</li>"
        )
        text_parts.append(f"\n{title} ({group})\n  {link}")
        text_parts.append(f"  Claude — relevant: {claude_v['strict_relevant']} — {claude_v['analysis']}")

        for model_name, v in r["others"].items():
            html_parts.append(
                f"<li><b>{model_name}</b> — relevant: {v['strict_relevant']} — {v['analysis']}</li>"
            )
            text_parts.append(f"  {model_name} — relevant: {v['strict_relevant']} — {v['analysis']}")
        html_parts.append("</ul>")

    html = f"<html><body>{''.join(html_parts)}</body></html>"
    text = "\n".join(text_parts)

    return {"subject": subject, "html": html, "text": text}


def send_email(msg, recipients=None):
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": recipients if recipients is not None else ALERT_EMAIL_TO,
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
        if _is_blocked_source(entry):
            print(f"    [blocked source] {entry.get('title', 'Untitled')} — {_entry_source_name(entry)}")
            continue
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

_RELEVANCE_SYSTEM_PROMPT = """You are a high-yield credit analyst screening news for a datacenter bond monitoring system.

You will be given a bond/issuer group (ticker(s) with coupon/maturity, tenant, and lease structure; issuer name; site location; whether this is a LOCAL/SITE-level search or a CORPORATE-level search) and a list of candidate news articles that matched a keyword search. The keyword search is deliberately broad and produces a lot of noise — coincidental keyword overlaps, generic legal-explainer content, unrelated companies, routine local news with no real connection to this issuer or site, and derivative commentary that isn't actual new reporting.

This feed generates two outputs from the same assessment: a STRICT digest (only genuinely material, primary, on-topic news) and a BROADER review digest (anything on-topic at all, for manual QC of whether the strict filter is too aggressive). To support both, score each article on three SEPARATE, independent criteria rather than one combined yes/no:

1. "on_topic": Is this article actually about this specific issuer, its tenant, or this specific site/location — not just a coincidental keyword match, not an unrelated company, not generic content (legal explainers, routine local news like sports/weather with no substantive tie)? A sector- or industry-trend piece that discusses a tenant/company as one example within a broader narrative about an entire category of companies (e.g. "neoclouds are getting bigger and riskier," "the AI datacenter boom faces headwinds") is NOT on_topic even if it names the tenant — it's commentary about a trend, not about this specific issuer's situation, unless it reports a fact specific to this issuer distinguishable from the general narrative. This is the only bar for "is this worth a human's attention to review at all."
2. "market_moving": Is it plausibly market-moving or credit-relevant for this bond? On weighting LOCAL vs. CORPORATE: LOCAL/SITE-level news (permitting/zoning votes or reversals, county/planning commission decisions, utility/interconnection disputes or delays, tax abatement votes, litigation tied to the specific site, water/power use disputes, organized local opposition affecting timeline) is very often the single most important, earliest credit signal for this kind of debt — apply a MODERATE bar here: genuine, confirmed site-specific developments count even if modest in scale. For CORPORATE-level news, apply a HIGHER bar: require a clear, specific, stated mechanism tying it to this bond's actual economics (tenant ability-to-pay, issuer financing, ratings, litigation, use-of-proceeds affecting this site). Valuation milestones, funding-round announcements, or "milestone reached" PR that state a headline number WITHOUT a specific stated mechanism connecting it to this bond's cash flows, collateral, or counterparty risk should be market_moving=false — a valuation figure alone doesn't tell you if lease terms or ability-to-pay changed.

CRITICAL — SITE-MATCHING FOR MULTI-SITE SPONSORS: some corporate parents sponsor multiple separate project-finance bonds secured by DIFFERENT physical sites (e.g. TeraWulf's WULF notes are secured by its Barker, NY site; its FLASHC notes by a different Abernathy, TX site — a news story about a third TeraWulf site, e.g. one in Hancock County, KY, is about neither). Each bond's "Site location(s)" is given in the bond detail above. If a CORPORATE-context article describes a development at a specific site, check whether that site matches the site(s) listed for the ticker(s) in this group:
- If the site matches (or the article is genuinely company-wide — overall earnings, corporate-level financing, executive changes, litigation against the parent entity itself, credit ratings on the parent) — proceed with the normal market_moving assessment.
- If the site does NOT match — it's a different, untracked site under the same sponsor — do not treat it as market_moving for this bond's specific collateral. Say so explicitly in the analysis (e.g. "this is TeraWulf's Hancock, KY site, not WULF's Barker, NY or FLASHC's Abernathy, TX sites — no direct collateral impact"), and only mark it relevant if you're treating it purely as weak, general sponsor-level context (which should still generally be market_moving=false unless the scale is large enough to plausibly affect the sponsor's overall ability to support all its project subsidiaries).
- Note: some tickers (e.g. CORZ) are themselves secured across multiple listed sites — for those, news about any of that ticker's own listed sites is legitimately relevant to that same bond; the mismatch case is specifically about a site that isn't listed for ANY ticker in this group at all.
3. "primary_incremental": Is this primary, incremental reporting — an actual new fact or development — rather than derivative commentary or a rehash? Mark false for: stock technical-analysis or macro-driven equity price commentary ("why X stock moved today", chart/momentum pieces, "stocks to watch" listicles, or pieces attributing stock price moves — for one name or several named together — to macro conditions like interest rates, Treasury yields, or broad risk sentiment, without reporting a company-specific new fact) — these are equity-market commentary reacting to price action or macro conditions, not primary news about a specific issuer's operations, financing terms, or credit profile, even when they name the specific tickers and cite real numbers; opinion/recap/"explainer" pieces restating previously reported facts; aggregator/wire rehashes with no new information beyond a prior article; sector-wide opinion/analysis pieces (op-eds, "state of the industry" pieces) that use a tenant as an illustrative example rather than reporting a new fact about that specific issuer; and sell-side analyst rating/price-target actions ("X maintains Buy rating, raises price target to $Y", coverage initiations, rating changes) — these reflect one analyst's valuation opinion, not a new fact about the issuer's operations, financing, or credit profile, regardless of which outlet reports it. Also watch for STALE PRIMARY COVERAGE: read the article's own text for internal date cues (e.g. "filed Monday", "announced earlier this week", "in a filing made public on [date]", "shares fell after Tuesday's disclosure") that indicate the underlying event actually happened noticeably earlier than the article's own publish date — this signals catch-up/secondary coverage of an already-disclosed fact, not the disclosure itself, even when the headline reads like breaking news ("Company X files for IPO") and the outlet is legitimate. Mark these primary_incremental=false unless the article itself adds a genuinely new fact beyond the earlier disclosure (updated terms, market reaction data, new figures not in the original disclosure).

Be reasonably generous on "on_topic" (that's the low bar for the review digest) but strict and precise on "market_moving" and "primary_incremental" (those gate the main alert). When genuinely uncertain on "on_topic," lean inclusive; when uncertain on the other two, lean toward false.

For each article, always include a brief one-sentence "analysis": if on_topic and market_moving and primary_incremental are all true, a bond-specific impact citing the ticker and relevant bond terms (e.g. "credit positive for the 6.25% MERIDI notes due 4/30/31, where Fluidstack sits under a Google-guaranteed triple-net lease — reduced counterparty risk on the lease servicing the notes"). Otherwise, a brief reason noting which criterion failed and why (e.g. "on-topic but not market-moving: valuation milestone with no stated mechanism", "on-topic but derivative: recap of already-reported facts", "off-topic: unrelated company, coincidental keyword match"). Never leave analysis empty.

Respond with ONLY a JSON array, no other text, no markdown code fences, one object per article in the same order given:
[{"index": 0, "on_topic": true, "market_moving": true, "primary_incremental": true, "analysis": "..."}, {"index": 1, "on_topic": true, "market_moving": false, "primary_incremental": true, "analysis": "..."}]"""


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Pricing as of Sep 2026, $/M tokens (input, output). Verify current rates
# before trusting the cost log over time — these are hardcoded estimates:
# Anthropic: https://www.anthropic.com/claude/haiku
# xAI: https://docs.x.ai/developers/models (Grok 4.1 Fast is the cheap tier)
# OpenAI: https://developers.openai.com/api/docs/pricing (gpt-5-mini)
_MODEL_PRICING = {
    "anthropic": (1.00, 5.00),
    "xai": (0.20, 0.50),
    "openai": (0.25, 2.00),
}

# Accumulates actual token usage per provider across a single run (reset
# at the top of main()), so we can log a real cost estimate at the end
# rather than guessing from character counts.
_usage_totals = {
    provider: {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    for provider in _MODEL_PRICING
}


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
    usage = data.get("usage", {})
    _usage_totals["anthropic"]["input_tokens"] += usage.get("input_tokens", 0)
    _usage_totals["anthropic"]["output_tokens"] += usage.get("output_tokens", 0)
    _usage_totals["anthropic"]["calls"] += 1
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def _call_openai_compatible(provider, api_url, api_key, model, system_prompt, user_prompt,
                             max_tokens=2000, token_param_name="max_tokens"):
    """xAI's Grok API is documented as OpenAI-SDK-compatible, so both Grok
    and actual OpenAI models are called through the same chat-completions
    request/response shape. provider is "xai" or "openai" — used only to
    file usage under the right cost bucket.

    token_param_name matters: OpenAI's GPT-5 family (and the older o1/o3
    reasoning models) reject the legacy "max_tokens" parameter outright
    with a 400 Bad Request and require "max_completion_tokens" instead;
    Grok still accepts "max_tokens". Each caller (_call_grok / _call_gpt)
    passes the right one for its provider.

    Retries once after a short pause on a 429 (rate limit / quota), since
    a single transient rate-limit hit shouldn't immediately fail open —
    this is common on newly-created API accounts sitting on a low usage
    tier, or accounts without a payment method on file (which OpenAI also
    surfaces as 429). If it fails twice, the caller's normal fail-open
    handling takes over."""
    last_exc = None
    for attempt in range(2):
        resp = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                token_param_name: max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60,
        )
        if resp.status_code == 429 and attempt == 0:
            print(f"[warn] {provider} returned 429 (rate limit/quota) — retrying once after 5s")
            time.sleep(5)
            continue
        try:
            resp.raise_for_status()
        except Exception as e:
            # Include the response body — it usually names the exact
            # problem (e.g. "Unsupported parameter: 'max_tokens'...")
            # far more precisely than the bare status-line exception does.
            last_exc = RuntimeError(f"{e} — response body: {resp.text[:500]}")
            break
        data = resp.json()
        usage = data.get("usage", {})
        _usage_totals[provider]["input_tokens"] += usage.get("prompt_tokens", 0)
        _usage_totals[provider]["output_tokens"] += usage.get("completion_tokens", 0)
        _usage_totals[provider]["calls"] += 1
        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
    raise last_exc if last_exc else RuntimeError(f"{provider} request failed after retry")


def _call_grok(system_prompt, user_prompt, max_tokens=2000):
    return _call_openai_compatible("xai", XAI_API_URL, XAI_API_KEY, XAI_MODEL, system_prompt, user_prompt,
                                    max_tokens, token_param_name="max_tokens")


def _call_gpt(system_prompt, user_prompt, max_tokens=2000):
    return _call_openai_compatible("openai", OPENAI_API_URL, OPENAI_API_KEY, OPENAI_MODEL, system_prompt, user_prompt,
                                    max_tokens, token_param_name="max_completion_tokens")


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
        locations = info.get("locations") or []
        loc_str = "; ".join(locations) if locations else "diffuse/many sites (not a single-site bond)"
        lines.append(
            f"- {t}: {info.get('coupon_maturity', 'n/a')} | "
            f"Tenant: {info.get('tenant', 'n/a')} | Lease: {info.get('lease', 'n/a')} | "
            f"Site location(s): {loc_str}"
        )
    return "\n".join(lines)


# Max candidates sent to Claude in a single call. A batch this size, each
# needing a JSON verdict object, comfortably fits within a few thousand
# output tokens; batches larger than this (which can happen when the raw,
# ungated search — see fetch_raw_location_news — hits a location with a
# lot of unrelated daily news volume) get split into multiple calls
# instead of risking a truncated/failed response that would otherwise
# fail-open an enormous batch into the review digest at once.
MAX_CANDIDATES_PER_CLAUDE_CALL = 25


def _assess_relevance_with(call_fn, provider_label, group_label, tickers, entries, context_type, previously_alerted_titles=None):
    """Provider-agnostic core of the relevance assessment — call_fn is
    _call_claude, _call_grok, or _call_gpt. Used both by the real gating
    pipeline (via assess_relevance, always Claude) and by
    cross_model_disagreement_report (via Grok/GPT, for comparison only).

    Returns a list of dicts, one per input entry, in the same order:
    [{"entry": entry, "on_topic": bool, "market_moving": bool,
      "primary_incremental": bool, "strict_relevant": bool,
      "broad_relevant": bool, "analysis": str}, ...]

    Splits into multiple calls if there are more than
    MAX_CANDIDATES_PER_CLAUDE_CALL entries. Fails open to "review only,
    not the main alert" on any error — this fail-open behavior applies
    even to Grok/GPT calls used purely for comparison, so a failed
    cross-check never accidentally suppresses or promotes anything in
    the real pipeline (which cross-model results never touch anyway)."""
    if not entries:
        return []

    if len(entries) > MAX_CANDIDATES_PER_CLAUDE_CALL:
        print(f"    [batching] {group_label} ({provider_label}): {len(entries)} candidates exceeds "
              f"{MAX_CANDIDATES_PER_CLAUDE_CALL}, splitting into multiple calls")
        results = []
        for start in range(0, len(entries), MAX_CANDIDATES_PER_CLAUDE_CALL):
            chunk = entries[start:start + MAX_CANDIDATES_PER_CLAUDE_CALL]
            results.extend(_assess_relevance_with(call_fn, provider_label, group_label, tickers, chunk,
                                                   context_type, previously_alerted_titles))
        return results

    article_lines = []
    for i, entry in enumerate(entries):
        title = entry.get("title", "")
        summary = (entry.get("summary", "") or "")[:500]
        article_lines.append(f"{i}. TITLE: {title}\n   SUMMARY: {summary}")

    if previously_alerted_titles:
        prior_block = (
            "\n\nHeadlines ALREADY SENT in the main alert for this bond group in the "
            f"last {RECENTLY_ALERTED_LOOKBACK_DAYS} days (do not re-alert on the same "
            "underlying event/story under different wording — check each candidate "
            "against these for a semantic match, not just exact text overlap; if a "
            "candidate covers the same fact already sent, mark primary_incremental=false "
            "unless it adds a genuinely new incremental development beyond what's listed "
            "here):\n" + "\n".join(f"- {t}" for t in previously_alerted_titles)
        )
    else:
        prior_block = ""

    user_prompt = (
        f"Search context: {context_type.upper()}\n"
        f"Bond group: {group_label}\n"
        f"Bond detail:\n{_bond_detail_lines(tickers)}"
        f"{prior_block}\n\n"
        f"Candidate articles:\n" + "\n".join(article_lines)
    )

    try:
        raw = call_fn(_RELEVANCE_SYSTEM_PROMPT, user_prompt, max_tokens=4096)
        results = _parse_json_array(raw)
    except Exception as e:
        print(f"[warn] {provider_label} relevance check failed for {group_label}: {e} "
              f"— routing all {len(entries)} candidate(s) to the review digest only "
              f"(fail-open never goes to the main alert, to avoid a technical failure "
              f"flooding the shared distribution)")
        return [{
            "entry": entry, "on_topic": True, "market_moving": False, "primary_incremental": False,
            "strict_relevant": False, "broad_relevant": True, "assessment_failed": True,
            "analysis": f"(unassessed — {provider_label} call failed; routed here for manual review rather than the main alert)",
        } for entry in entries]

    by_index = {}
    for r in results:
        idx = r.get("index")
        if idx is None or not isinstance(idx, int) or not (0 <= idx < len(entries)):
            continue
        by_index[idx] = r

    verdicts = []
    for i, entry in enumerate(entries):
        r = by_index.get(i)
        if r is None:
            verdicts.append({
                "entry": entry, "on_topic": False, "market_moving": False, "primary_incremental": False,
                "strict_relevant": False, "broad_relevant": False, "assessment_failed": True,
                "analysis": f"(no verdict returned by {provider_label})",
            })
        else:
            on_topic = bool(r.get("on_topic"))
            market_moving = bool(r.get("market_moving"))
            primary_incremental = bool(r.get("primary_incremental"))
            verdicts.append({
                "entry": entry,
                "on_topic": on_topic,
                "market_moving": market_moving,
                "primary_incremental": primary_incremental,
                "strict_relevant": on_topic and market_moving and primary_incremental,
                "broad_relevant": on_topic,
                "assessment_failed": False,
                "analysis": r.get("analysis", ""),
            })
    return verdicts


def assess_relevance(group_label, tickers, entries, context_type, previously_alerted_titles=None):
    """Assesses every keyword-matched candidate for genuine relevance,
    bond-specificity, and materiality, using Claude — this is the one
    that actually gates the main alert and review digest. context_type
    is "corporate" or "local" — it changes how strictly the relevance
    bar is applied (see system prompt). previously_alerted_titles
    (optional) is a list of headlines already sent in the main alert for
    this same bond group in the recent past — passed as context so
    Claude can catch the same underlying story reworded by a different
    outlet (fuzzy title-matching alone misses this when the wording
    differs enough, e.g. "SB Energy files for IPO" vs "American data
    center operator SB Energy is planning an IPO").

    See _assess_relevance_with for the return shape and batching/fail-open
    behavior, which this delegates to."""
    return _assess_relevance_with(_call_claude, "Claude", group_label, tickers, entries, context_type,
                                   previously_alerted_titles)


def cross_model_disagreement_report(group_label, tickers, entries, context_type, claude_verdicts,
                                     previously_alerted_titles=None):
    """Runs the same candidate batch through whichever of Grok/GPT are
    configured (via XAI_API_KEY / OPENAI_API_KEY), using the identical
    prompt and schema Claude uses, and returns a list of disagreement
    records — one per article where at least one other model's
    strict_relevant call differs from Claude's.

    This is purely a diagnostic/audit feature: it never affects what
    goes into the main alert or review digest (Claude's judgment remains
    authoritative for actual filtering). It exists to surface cases
    worth manually reviewing — if multiple models disagree with Claude
    on the same article, that's a stronger signal for prompt tuning than
    Claude's judgment alone.

    Returns [] immediately if neither XAI_API_KEY nor OPENAI_API_KEY is
    configured, so this is a no-op by default."""
    if not entries or not (XAI_API_KEY or OPENAI_API_KEY):
        return []

    other_verdicts = {}
    if XAI_API_KEY:
        other_verdicts["Grok"] = _assess_relevance_with(
            _call_grok, "Grok", group_label, tickers, entries, context_type, previously_alerted_titles)
    if OPENAI_API_KEY:
        other_verdicts["GPT"] = _assess_relevance_with(
            _call_gpt, "GPT", group_label, tickers, entries, context_type, previously_alerted_titles)

    records = []
    for i, claude_v in enumerate(claude_verdicts):
        row_disagrees = False
        other_calls = {}
        for model_name, verdicts in other_verdicts.items():
            if i >= len(verdicts):
                continue
            v = verdicts[i]
            if v.get("assessment_failed"):
                # The call to this provider failed for this article — we
                # genuinely don't know its opinion, so it's excluded from
                # the comparison entirely rather than silently counted as
                # agreement or disagreement based on a placeholder verdict.
                continue
            other_calls[model_name] = v
            if v["strict_relevant"] != claude_v["strict_relevant"]:
                row_disagrees = True
        if row_disagrees:
            records.append({
                "entry": claude_v["entry"],
                "group_label": group_label,
                "claude": claude_v,
                "others": other_calls,
            })
    return records


def main():
    for provider in _usage_totals:
        _usage_totals[provider] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

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
        try:
            entries += fetch_raw_ticker_news(parent)
        except Exception as e:
            print(f"[warn] raw ticker fetch failed for {parent}: {e}")
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
        try:
            entries += fetch_raw_location_news(location)
        except Exception as e:
            print(f"[warn] raw location fetch failed for {location}: {e}")
        entries += fetch_curated_entries(location)
        fresh = _dedupe_fresh(entries, seen, seen_titles, f"local:{location}")
        if fresh:
            new_local[location] = fresh
        time.sleep(REQUEST_DELAY_SECONDS)

    seen = trim_seen(seen)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Assessing relevance with Claude...")

    strict_corporate, strict_local = {}, {}
    broad_corporate, broad_local = {}, {}  # on-topic but excluded from strict — for QC review only
    disagreement_records = []  # cross-model audit only, never gates the real emails

    def _log_split_and_record(verdicts, tag):
        strict_items = []
        broad_extra_items = []
        for v in verdicts:
            title = v["entry"].get("title", "Untitled")
            link = v["entry"].get("link", "")
            log_tag = "KEPT" if v["strict_relevant"] else "rejected"
            print(f"    [{log_tag}] {title}")
            print(f"           {link}")
            print(f"           reason: {v['analysis']}")
            if v["strict_relevant"]:
                strict_items.append((v["entry"], v["analysis"]))
            elif v["broad_relevant"]:
                broad_extra_items.append((v["entry"], v["analysis"]))
            # Record the verdict back onto the seen entry so future runs
            # can tell Claude "this was already sent in the main alert" —
            # this is what powers cross-run semantic-duplicate detection.
            key = article_key(v["entry"])
            if key in seen:
                seen[key]["strict_relevant"] = v["strict_relevant"]
        return strict_items, broad_extra_items

    for parent in list(new_corporate.keys()):
        entries = new_corporate[parent]
        print(f"  assessing corporate: {parent} ({len(entries)} candidate(s))")
        prior_titles = _recent_alerted_titles(seen, f"corp:{parent}")
        verdicts = assess_relevance(parent, PARENT_GROUPS[parent], entries, context_type="corporate",
                                     previously_alerted_titles=prior_titles)
        strict_items, broad_extra_items = _log_split_and_record(verdicts, f"corp:{parent}")
        if strict_items:
            strict_corporate[parent] = strict_items
        if broad_extra_items:
            broad_corporate[parent] = broad_extra_items
        disagreement_records.extend(cross_model_disagreement_report(
            parent, PARENT_GROUPS[parent], entries, "corporate", verdicts, previously_alerted_titles=prior_titles))

    for location in list(new_local.keys()):
        entries = new_local[location]
        print(f"  assessing local: {location} ({len(entries)} candidate(s))")
        prior_titles = _recent_alerted_titles(seen, f"local:{location}")
        verdicts = assess_relevance(location, LOCATION_GROUPS[location], entries, context_type="local",
                                     previously_alerted_titles=prior_titles)
        strict_items, broad_extra_items = _log_split_and_record(verdicts, f"local:{location}")
        if strict_items:
            strict_local[location] = strict_items
        if broad_extra_items:
            broad_local[location] = broad_extra_items
        disagreement_records.extend(cross_model_disagreement_report(
            location, LOCATION_GROUPS[location], entries, "local", verdicts, previously_alerted_titles=prior_titles))

    save_seen(seen)

    if strict_corporate or strict_local:
        msg = build_email(strict_corporate, strict_local)
        send_email(msg, recipients=ALERT_EMAIL_TO)
        total = sum(len(v) for v in strict_corporate.values()) + sum(len(v) for v in strict_local.values())
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sent main alert with {total} new item(s).")
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No new items for the main alert this run.")

    if broad_corporate or broad_local:
        review_msg = build_email(
            broad_corporate, broad_local,
            subject_prefix="HY Datacenter News — Broad Review (QC)",
            intro="On-topic items excluded from the main alert — for reviewing whether the filter is too strict",
        )
        send_email(review_msg, recipients=REVIEW_EMAIL_TO)
        total = sum(len(v) for v in broad_corporate.values()) + sum(len(v) for v in broad_local.values())
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sent review digest with {total} excluded-but-on-topic item(s) to {REVIEW_EMAIL_TO}.")
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No excluded-but-on-topic items for the review digest this run.")

    if disagreement_records:
        disagreement_msg = build_disagreement_email(disagreement_records)
        send_email(disagreement_msg, recipients=CROSS_MODEL_REPORT_EMAIL_TO)
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sent cross-model disagreement report with "
              f"{len(disagreement_records)} item(s) to {CROSS_MODEL_REPORT_EMAIL_TO}.")
    elif XAI_API_KEY or OPENAI_API_KEY:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No cross-model disagreements this run.")

    total_cost = 0.0
    for provider, totals in _usage_totals.items():
        if totals["calls"] == 0:
            continue
        in_rate, out_rate = _MODEL_PRICING[provider]
        cost = (totals["input_tokens"] / 1_000_000 * in_rate) + (totals["output_tokens"] / 1_000_000 * out_rate)
        total_cost += cost
        print(f"[{datetime.now(timezone.utc).isoformat()}] {provider} usage this run: {totals['calls']} call(s), "
              f"{totals['input_tokens']:,} input tok + {totals['output_tokens']:,} output tok "
              f"≈ ${cost:.5f} (at ${in_rate}/M in, ${out_rate}/M out — verify current pricing periodically).")
    if sum(t["calls"] for t in _usage_totals.values()) > 1:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Total LLM cost this run across all providers: ≈ ${total_cost:.5f}")


if __name__ == "__main__":
    main()
