# HY Datacenter News Monitor

Hourly news check across your tracked HY datacenter bonds — both
**corporate-level** news and **local/site-level** news (the specific
city/county where each deal's collateral sits). Runs as a Railway Cron Job.

## Why the local-news layer matters

Permitting, rezoning, tax abatement votes, substation/interconnection
delays, water-use disputes, and community opposition often show up in
local paper / county-commissioner-meeting coverage well before it hits
national outlets — and for site-specific project finance debt, that's
frequently the more decision-relevant signal.

## Tracked bonds

| Ticker | Issuing Entity | Parent | Site Location |
|---|---|---|---|
| APLD | Applied Digital - APLD ComputeCo LLC | Applied Digital | Ellendale, ND |
| PFORGE | Applied Digital - APLD ComputeCo 2 LLC | Applied Digital | Harwood, ND |
| ELNFOR | Applied Digital - APLD ComputeCo 3 LLC | Applied Digital | Ellendale, ND |
| CIFR | Cipher Digital - Cipher Compute LLC | Cipher Mining | Colorado City, TX |
| BLKPRL | Cipher Digital - Black Pearl ComputeCo LLC | Cipher Mining | Wink, TX |
| STNGRY | Cipher Digital - Stingray ComputCo LLC | Cipher Mining | Andrews, TX |
| CORZ | Core Scientific Inc (Core Scientific Finance LLC) | Core Scientific | Denton TX; Dalton GA; Muskogee OK; Marble NC; Austin TX |
| CRWV | CoreWeave Inc | CoreWeave | Various (41 datacenters — not geo-searched) |
| EDGCOM | Edged Compute LLC | Edged Compute | Atlanta, GA; Chicago, IL |
| GALAXY | Galaxy Helios Data Centers II LLC | Galaxy Digital | Dickens County, TX |
| MERIDI | Next Frontier/Fluidstack JV - Meridian Arc Holdco LLC | Next Frontier / Fluidstack JV | New Lebanon, Sullivan Cty, IN |
| ELKGVP | Prime Data Centers, LLC - Elk Grove Village Property LLC | Prime Data Centers | Elk Grove Village, IL |
| SECMOS | SB Energy - SE Cosmos, LLC | SB Energy | Austin, TX |
| TRACTC | Tract Capital/Fleet Data Centers - SV RNO Property Owner 1, LLC | Tract / Fleet Data Centers | Storey County, NV |
| TRACTD | Tract Capital/Fleet Data Centers - PR RNO Property Owner 1, LLC | Tract / Fleet Data Centers | Storey County, NV |
| WULF | TeraWulf - WULF Compute LLC | TeraWulf | Barker, NY |
| FLASHC | TeraWulf/Fluidstack JV - Flash Compute LLC | TeraWulf | Abernathy, TX |
| YNDRDC | Yondr Group - Yondr JK 1, LLC | Yondr Group | Loudoun County, VA |

To add/remove bonds or fix a location, edit the `BONDS` dict at the top of
`monitor.py`.

## How it works

Three layers of search, all feeding into a "Local / Site News" section:

1. **Corporate query** per unique parent company (12 unique). Tickers under
   the same parent (like Applied Digital's 3 bonds) share one query. A few
   parent labels are my own shorthand rather than real searchable names
   (e.g. "Tract / Fleet Data Centers"), so those use hand-tuned overrides
   (`CORPORATE_SEARCH_OVERRIDES`) pointed at the actual company name used
   in press coverage.
2. **Local query** per unique site location (19 unique), narrowed with
   keywords covering permitting/zoning, tax/fiscal, utility & power
   infrastructure, and litigation/regulatory disputes — the categories
   that tend to move HY credit views on site-specific project finance debt.
   Also folds in **tenant/hyperscaler and grid-interconnect terms**
   (`TENANT_KEYWORDS`) researched from the lease detail table — e.g.
   Wink/Andrews, TX (AWS tenant, ERCOT), Barker, NY (Core42/G42 + Fluidstack
   tenants, NYISO), Loudoun County, VA (Oracle tenant, PJM Dominion) — since
   tenant-specific and interconnection-queue coverage is often the most
   material site-level news but wouldn't otherwise surface from generic
   zoning/tax keywords alone.
3. **Site-specific terms** (`SITE_KEYWORDS`) — researched utility company
   names, project/campus nicknames, and county names per site (e.g.
   TeraWulf's Barker, NY site is publicly known as "Lake Mariner"; Dalton,
   GA is served by the city's own "Dalton Utilities"). Searched directly,
   without requiring the town name to co-occur, since a lot of trade-press
   coverage uses the project or utility name instead of the town.
4. **Curated feeds** (`CURATED_FEEDS`) — direct RSS polls of specific
   outlets known to cover a site closely (mostly state-level nonprofit
   newsrooms: North Dakota Monitor, Georgia Recorder, Oklahoma Voice, NC
   Newsline, Indiana Capital Chronicle, Virginia Mercury, Capitol News
   Illinois, Nevada Independent), filtered by keyword match. This is a
   higher-reliability supplement to Google News search, since smaller
   regional outlets can be slow to surface or rank low in search results —
   this is what originally caught the NV Energy/Tract lawsuit story.
5. **Relevance + impact analysis (Claude)** — the four layers above are a
   recall tool, not a precision tool: keyword matches produce real noise
   (a "lawsuit" keyword can just as easily hit a generic legal-blog
   explainer about crypto disclosure law as an actual lawsuit against a
   tenant). Every batch of keyword-matched candidates for a bond/location
   group is sent to Claude, which decides (a) is this actually about this
   specific issuer/site, not a coincidental keyword overlap, and (b) is it
   plausibly market-moving/credit-relevant — then writes a 1-2 sentence
   bond-specific impact note for anything that passes. Only items that
   pass both checks make it into the email. If the Claude call fails for
   any reason, that group's candidates are kept unfiltered (fails open,
   logged as a warning) rather than silently dropping the whole run's
   alerts.

All four layers use Google News RSS or direct outlet RSS (free, no API
key). Every article's link is hashed and checked against a persisted
`seen_articles.json` state file, so re-runs only alert on genuinely new
stories. Old entries are trimmed after 14 days. Emails a single digest per
run, split into "Corporate News" and "Local / Site News" sections, via
the Resend API — only sent if there's something new.

**Quality controls:**
- **Hard 2-week age cutoff** — no article older than 14 days can appear in
  an alert, regardless of source. Google News's `when:1d` filter is
  best-effort, not a guarantee, and the site-specific/curated-feed layers
  have no date filtering of their own, so this is enforced directly on
  each entry's published date.
- **Anchor-term requirement** — site-specific and curated-feed matches
  require a data-center/energy anchor term (`data center`, `datacenter`,
  `compute`, `hyperscale`, plus the local keyword list) to co-occur with
  the specific identifying term. Several utility/county names are common
  enough to appear in unrelated local news on their own — e.g. a bare
  "Whitfield County" match once pulled in a high-school volleyball
  recap — so those terms alone are no longer sufficient.
- **Fuzzy headline dedup** — the same story from two outlets (e.g. a wire
  story picked up by both a local paper and a national one) often has
  different URLs and slightly reworded headlines, which URL-hash dedup
  alone doesn't catch. Every new article's title is normalized (Google
  News's " - Source Name" suffix stripped, lowercased, punctuation
  collapsed) and compared against previously-seen titles using
  `difflib.SequenceMatcher`; anything ≥85% similar is treated as the same
  story and suppressed, even though the link differs. This persists
  across runs via `seen_articles.json`, same as the URL-based dedup.

## Cron schedule / quiet hours

Railway cron always evaluates in **UTC**, with no timezone override
available. To run hourly from 6am–6pm Eastern and stay silent overnight,
set the Cron Schedule (Settings → Cron Schedule) to:

```
0 10-22 * * *
```

(6am ET = 10:00 UTC, 6pm ET = 22:00 UTC, during Eastern Daylight Time.)

**This needs manual updating twice a year for DST.** When clocks fall
back (EST = UTC-5, typically early November), change it to:

```
0 11-23 * * *
```

and back to `0 10-22 * * *` when clocks spring forward again in March.
Railway has no timezone-aware cron option, so there's no way to avoid
this without running a separate always-on scheduler process.

**Coverage gaps to know about:** `SITE_KEYWORDS` and `CURATED_FEEDS` are
populated for most sites but not exhaustively verified — the curated feed
URLs are researched (based on known outlet patterns) but not all
individually load-tested, so if one comes back empty/errors in the logs,
that feed's URL likely needs correcting. A few sites (Dickens County TX,
Colorado City TX, Wink TX, Andrews TX, Abernathy TX) don't yet have a
curated feed — Texas Tribune covers data center/ERCOT issues well
statewide and would be a good one to add if useful.

## Why Resend instead of Gmail SMTP

Railway blocks outbound SMTP (ports 465/587/2525) entirely on Free, Trial,
and Hobby plans — it's only available on Pro and above. Gmail SMTP will
connect fine when you test locally (your Mac isn't behind that block) but
will fail with `OSError: [Errno 101] Network is unreachable` once deployed
on Railway. Rather than pay for Pro just to unblock SMTP, this uses
[Resend](https://resend.com)'s HTTPS API instead — HTTPS traffic isn't
blocked, and their free tier (3,000 emails/month) is far more than an
hourly alert job needs.

## 1. Resend setup

1. Sign up at https://resend.com (free).
2. **Add and verify a sending domain** at https://resend.com/domains —
   you'll add a few DNS records (TXT/CNAME) at your domain registrar.
   If you don't want to verify a domain yet, Resend also gives you a
   test sending address (`onboarding@resend.dev`) that works immediately,
   but can only send to the email address on your Resend account (not
   arbitrary recipients like your Mizuho address) — fine for an initial
   smoke test, not for the real recipient list.
3. Create an API key at https://resend.com/api-keys — copy it, you'll
   paste it into Railway as `RESEND_API_KEY`.

## 2. Push this to a GitHub repo

Railway deploys from a GitHub repo (or use the Railway CLI to deploy this
folder directly).

## 3. Create the Railway project

1. https://railway.app → New Project → Deploy from GitHub repo.
2. Railway auto-detects Python via `requirements.txt` — no extra build config needed.

## 4. Set environment variables

Service → **Variables**:

- `RESEND_API_KEY`
- `EMAIL_FROM` (e.g. `alerts@yourdomain.com`, must be on the verified domain)
- `ALERT_EMAIL_TO`
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com/settings/keys.
  Used for the relevance/impact-analysis step. Cost is low at this volume:
  each API call only fires for groups that actually have new candidate
  articles (typically a handful per run, often zero), using Haiku (the
  cheapest current model).
- `SEEN_FILE_PATH` = `/data/seen_articles.json`

## 5. Attach a Volume (important — persists dedupe state)

Cron containers are ephemeral. Without a volume, `seen_articles.json`
resets every run and you'll get repeat alerts.

1. Service → **Settings** → **Volumes** → **New Volume**.
2. Mount path: `/data`.
3. Confirm `SEEN_FILE_PATH` points inside that mount.

## 6. Set the start command

Service → **Settings** → **Deploy** → **Custom Start Command**:

```
python monitor.py
```

## 7. Set the Cron Schedule

Service → **Settings** → **Cron Schedule** — set this in the dashboard, not
in `railway.json` (Railway's config-as-code cron field has had reliability
issues as of late 2025/2026).

Hourly:

```
0 * * * *
```

## 8. Test locally first

```bash
pip install -r requirements.txt
export RESEND_API_KEY="re_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export EMAIL_FROM="alerts@yourdomain.com"
export ALERT_EMAIL_TO="you@example.com"
export ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx"
python monitor.py
```

You'll see a log line: "Sent alert with N new item(s)" or "No new items."

## Tuning notes

- **~60 total RSS calls per run** (12 corporate + 19 local + 18 site-specific
  + 11 curated feed polls), spaced 1.5s apart (~90s total runtime) — well
  within Google News RSS's practical rate limits at hourly cadence.
- If a specific location is too noisy or too quiet, adjust its query
  precision in `LOCAL_KEYWORDS` or add a more specific place name (e.g.
  swap "Austin, Texas" for a more precise sub-area if it's picking up
  unrelated Austin news).
- **Bloomberg / 8-K / ratings actions**: current setup is free,
  web-search-based. If you want ratings actions or primary filings
  specifically, that needs a paid news API or Bloomberg API integration —
  happy to add that layer later.
