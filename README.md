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
5. **Raw, ungated recall search** — every other layer above requires
   co-occurrence with some anchor vocabulary (zoning, utility, lawsuit,
   etc.), which is good for precision but means a genuinely relevant
   article that just doesn't happen to use any of that vocabulary would
   never be found at all — a search-layer coverage gap that no amount of
   downstream filtering can fix. This layer searches only the bare
   location name (`fetch_raw_location_news`) or, for parents that
   actually trade publicly, the real equity ticker
   (`fetch_raw_ticker_news`, via `PUBLIC_TICKER_OVERRIDE` — note most of
   the "tickers" in `BONDS` are internal bond/SPV shorthand, not real
   stock symbols, so this only applies to APLD, CIFR, CORZ, CRWV, WULF,
   and Galaxy Digital's real ticker GLXY). This is deliberately noisier
   than the other layers; the extra noise is handled downstream by the
   same Claude assessment as everything else, so a genuine hit here can
   land in the main alert, not just the review digest — this layer isn't
   just diagnostic, it actively closes real coverage gaps.

   **Exception**: `RAW_LOCATION_SEARCH_EXCLUDE` skips this layer entirely
   for large/generic metro locations (currently Austin, TX and Chicago,
   IL) where a bare-name search returns enormous unrelated daily news
   volume (Tesla launch events, obituaries, local sports, everything) —
   exactly the flood the anchor-term gating on `fetch_local_news` exists
   to prevent. A batch large enough can overwhelm a single Claude call
   and previously triggered a fail-open that dumped dozens of irrelevant
   articles straight into the main alert; see the batching and fail-open
   fixes below for the general-purpose backstops, and this exclusion for
   removing the risk at the source for known-bad locations.
6. **Relevance + impact analysis (Claude)** — the five layers above are a
   recall tool, not a precision tool: keyword matches produce real noise
   (a "lawsuit" keyword can just as easily hit a generic legal-blog
   explainer about crypto disclosure law as an actual lawsuit against a
   tenant). Every batch of keyword-matched candidates for a bond/location
   group is sent to Claude, which scores each article on three
   *independent* criteria rather than one combined yes/no:
   - `on_topic` — is this actually about this specific issuer/site, not a
     coincidental keyword overlap or unrelated company?
   - `market_moving` — is it plausibly credit-relevant for this bond?
     Local/site-level news (permitting, zoning, utility disputes,
     litigation tied to the site) is held to a moderate bar, since it's
     often the earliest and most important signal for this kind of debt.
     Corporate-level news (financing, valuation milestones, general PR)
     is held to a higher bar — it needs a stated mechanism tying it to
     this bond's actual economics, not just a headline number. **This
     check also verifies the article is actually about the right
     physical site**: some sponsors (TeraWulf, Applied Digital, Cipher
     Mining) run multiple separate project-finance bonds secured by
     different sites, and a corporate-level news search can surface a
     story about an untracked sibling site under the same parent — e.g.
     a "TeraWulf" search once returned a story about a Hancock County,
     KY project, which is neither WULF's Barker, NY site nor FLASHC's
     Abernathy, TX site. Each ticker's actual site location (from
     `BONDS`) is included in the prompt precisely so Claude can catch
     this and mark it not market-moving for that specific bond's
     collateral, rather than assuming any news about the parent company
     applies to every bond it sponsors.
   - `primary_incremental` — is this original reporting of a new fact,
     not derivative commentary (stock technical-analysis, "why X stock
     moved today" pieces) or a rehash/recap of already-reported facts?

   **Two emails come out of this**: the main alert requires all three
   criteria (`strict_relevant`), and a second **review digest** requires
   only `on_topic` — so anything that was at least genuinely about a
   tracked issuer/site, but got excluded from the main alert for not
   being material or not being primary reporting, shows up there instead
   with a note on which criterion it failed. This is a QC tool for
   tuning the filter itself: if something in the review digest looks
   like it should have been in the main alert, that's a signal to adjust
   the prompt. The main alert goes to `ALERT_EMAIL_TO`; the review digest
   goes to `REVIEW_EMAIL_TO` (defaults to just the first `ALERT_EMAIL_TO`
   address, not the whole distribution).

   For every candidate — kept, reviewed, or fully rejected — the full
   verdict and reason gets printed to the Railway deploy logs, so a
   quiet run (no email at all) can be audited directly: did the keyword
   search find nothing, or did Claude reject something that shouldn't
   have been rejected?

   If the Claude call fails for any reason, that group's candidates are
   routed to the review digest only — never the main alert — logged as a
   warning rather than silently dropping the whole run's alerts (see the
   fail-open note further down for why the main alert specifically is
   protected from this).

7. **Cross-model disagreement report (optional)** — a diagnostic-only
   feature, off by default. If `XAI_API_KEY` and/or `OPENAI_API_KEY` are
   set, every candidate batch that Claude assesses is also sent to Grok
   and/or GPT using the identical prompt and schema
   (`cross_model_disagreement_report`). Wherever another model's
   `strict_relevant` call differs from Claude's, it shows up in a third
   email — recipients set via `CROSS_MODEL_REPORT_EMAIL_TO` (defaults to
   `REVIEW_EMAIL_TO`) — only sent when there's at least one actual
   disagreement. This never changes what goes into the main alert or
   review digest; Claude's judgment remains the one that actually gates
   anything. It exists purely to surface cases where multiple models
   disagree with Claude's call, which is a stronger tuning signal than
   Claude's judgment reviewed alone. Leave both keys unset to skip this
   entirely — nothing else about the pipeline changes.

All four layers use Google News RSS or direct outlet RSS (free, no API
key). Every article's link is hashed and checked against a persisted
`seen_articles.json` state file, so re-runs only alert on genuinely new
stories. Old entries are trimmed after 14 days. Emails a single digest per
type per run, split into "Corporate News" and "Local / Site News" sections
within the main and review emails, via the Resend API — only sent if
there's something new for that email type.

**Quality controls:**
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
- **Cross-run semantic-duplicate memory** — fuzzy title matching only
  catches near-identical wording. It won't catch "SB Energy files for
  IPO" and "American data center operator SB Energy is planning an IPO"
  as the same story, even though they clearly are — the wording differs
  too much. To catch this, every bond group's *already-alerted* headlines
  (from `strict_relevant` verdicts, recorded back into `seen_articles.json`
  after each run) are passed to Claude as context for the next
  `RECENTLY_ALERTED_LOOKBACK_DAYS` (10) days, with instructions to check
  new candidates for a semantic — not just textual — match, and mark a
  re-reported version of an already-covered fact as non-incremental. This
  is what actually stops the same underlying story from re-appearing in
  the main alert every time a different outlet picks it up.
- **3-day hard age cutoff** (`MAX_ARTICLE_AGE_DAYS`) — tightened from an
  earlier 14-day version after a Denton, TX article with an actual byline
  of Aug 21 surfaced in a Sept 3 run (13 days old — technically inside a
  14-day window, but obviously stale for a feed that runs hourly).
  Google's `when:1d` filter is best-effort, not exact, so this is the
  real enforcement point; there's little reason a genuinely new story
  should be more than a couple days old by the time some layer surfaces
  it, given the hourly cadence.
- **Hard source blocklist** (`BLOCKED_SOURCES`) — some outlets are
  structurally secondary/opinion by nature (contributor-driven stock
  commentary, technical analysis, content-mill sites), not primary
  reporting, regardless of what any individual article says. Rather than
  relying on the LLM to catch every instance, these are blocked outright
  before an article is even considered a candidate: currently Seeking
  Alpha, Motley Fool, Zacks, Benzinga, InvestorPlace, MarketBeat, Simply
  Wall St, GuruFocus, Insider Monkey, TipRanks, Barchart, 24/7 Wall St.
  Matched against the article's source name (or the " - Source Name"
  suffix Google News appends to titles), not the link domain — Google
  News RSS wraps links through news.google.com, so the true publisher
  domain usually isn't recoverable from the link itself. Add more names
  to the `BLOCKED_SOURCES` set in `monitor.py` as needed; blocked items
  are logged as `[blocked source]` in the Railway deploy logs.
- **Batch-size protection** (`MAX_CANDIDATES_PER_CLAUDE_CALL`) — an
  unusually large candidate batch (63 articles once got pulled for
  Austin, TX in one run, driven by an unrelated Tesla launch event that
  happened to dominate that day's local news) risks overflowing a single
  Claude call's response and causing it to fail. Batches larger than 25
  are automatically split into multiple calls rather than risking that.
- **Fail-open never reaches the main alert.** If a Claude call fails
  (bad key, API outage, oversized/truncated response) after retries,
  that batch's candidates are routed to the QC review digest only —
  unfiltered, tagged `(unassessed — Claude call failed)` — never to the
  main alert that goes to the full distribution. A previous version of
  this logic defaulted to "keep everything, send it," which once dumped
  dozens of completely unrelated articles (Tesla news, obituaries, local
  sports) into the main alert when a batch failed. A noisy review digest
  that only reaches you is a recoverable failure mode; flooding
  colleagues' inboxes with unfiltered junk is not.

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
- `REVIEW_EMAIL_TO` (optional) — recipient(s) for the broader QC/review
  digest (see below). Defaults to just the first `ALERT_EMAIL_TO` address
  if unset, since this is a tuning tool, not something the full
  distribution needs.
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com/settings/keys.
  Used for the relevance/impact-analysis step. Cost is low at this volume:
  each API call only fires for groups that actually have new candidate
  articles (typically a handful per run, often zero), using Haiku (the
  cheapest current model).
- `XAI_API_KEY` (optional) — from https://console.x.ai. Enables the
  cross-model disagreement report (see below). Omit entirely to skip
  this feature; nothing else changes if it's unset.
- `OPENAI_API_KEY` (optional) — from https://platform.openai.com. Same
  purpose as `XAI_API_KEY`, for GPT instead of Grok. Either, both, or
  neither can be set independently.
- `CROSS_MODEL_REPORT_EMAIL_TO` (optional) — recipient(s) for the
  disagreement report. Defaults to `REVIEW_EMAIL_TO` if unset.
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

- **~90 total RSS calls per run** (13 corporate + 20 local + 6 raw ticker +
  19 site-specific + 12 curated feed polls + 20 raw location), spaced 1.5s
  apart between locations/parents — a few minutes total runtime, still
  well within Google News RSS's practical rate limits at hourly cadence.
- If a specific location is too noisy or too quiet, adjust its query
  precision in `LOCAL_KEYWORDS` or add a more specific place name (e.g.
  swap "Austin, Texas" for a more precise sub-area if it's picking up
  unrelated Austin news).
- **Bloomberg / 8-K / ratings actions**: current setup is free,
  web-search-based. If you want ratings actions or primary filings
  specifically, that needs a paid news API or Bloomberg API integration —
  happy to add that layer later.
