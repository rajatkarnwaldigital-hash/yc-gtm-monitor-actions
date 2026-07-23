#!/usr/bin/env python3
"""
Daily YC GTM job monitor.

Pulls every company from the YC public API, scrapes each company's YC page
for open roles + founders, diffs new GTM-relevant roles against seen_jobs.json,
generates a personalized outreach message per founder via Claude, and emails
a digest via Gmail SMTP.

Builds on the page-scraping logic (founder cards, job links) proven out in
yc_prospector.py.

Note: the Railway deployment of this same script (github.com/rajatkarnwaldigital-hash/yc-gtm-monitor)
sends through Resend's HTTP API instead, because Railway blocks outbound SMTP
entirely. GitHub Actions' hosted runners don't have that restriction, so plain
Gmail SMTP works fine here and doesn't require anyone forking this repo to own
a domain just to send themselves a digest email.
"""

import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

YC_API_BASE = "https://api.ycombinator.com/v0.1/companies"

# Point this at a mounted Railway Volume path (e.g. /data/seen_jobs.json) so
# state survives across cron runs — Railway cron containers do not retain a
# local filesystem between invocations unless a Volume is attached.
SEEN_JOBS_PATH = Path(os.environ.get("SEEN_JOBS_FILE", "seen_jobs.json"))

SCRAPE_WORKERS = 20
PAGE_TIMEOUT = 30
SITE_TIMEOUT = 15
API_SLEEP = 0.05

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

GTM_KEYWORDS = [
    "growth", "gtm", "go-to-market", "sales", "marketing", "founding ae",
    "sdr", "bdr", "revenue", "demand gen", "outbound", "automation",
    "lead gen", "business development", "founding account executive",
    "founding sales",
]
GTM_PATTERN = re.compile("|".join(re.escape(k) for k in GTM_KEYWORDS), re.IGNORECASE)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
HUNTER_MIN_CONFIDENCE = 70  # only include emails at or above this score
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")

CLAUDE_MODEL = "claude-sonnet-4-6"
# Verification is a small, cheap yes/no + one-sentence-reason check run once per
# candidate research fact — a fast/cheap model is intentional here, not a cost cut
# on the part that matters (the main message-drafting call keeps CLAUDE_MODEL).
VERIFY_MODEL = "claude-haiku-4-5-20251001"

# Every client.messages.create() call in this file does a single-shot, format-
# constrained task (classify a fact, pick a name, draft a short message) with no
# need for multi-step reasoning. Passed explicitly on every call: Haiku 4.5 (and
# possibly other newer models) defaults to adaptive extended thinking when this
# is omitted, which was silently eating the entire max_tokens budget on internal
# reasoning before emitting any visible text — the exact cause of every
# verification call coming back as an empty, unparseable response. See
# verify_fact() for the failure signature this produced.
THINKING_DISABLED = {"type": "disabled"}

JD_MAX_CHARS = 6000  # plenty for a job description; keeps the prompt bounded
EXA_TIMEOUT = 20
EXA_RESULTS_PER_QUERY = 2
EXA_TEXT_MAX_CHARS = 2000  # per-result excerpt handed to the verifier

MIN_ROLES_FOR_RANKING = 4  # below this, every lead is already "the top lead"

# Every message prompt below opens the same way and ends with the same
# first-person rule, on purpose: a prior version left "first vs third person"
# ambiguous ("on behalf of Rajat"), and the model split roughly evenly between
# writing AS Rajat and writing ABOUT Rajat in the same run, sometimes for two
# founders at the same company. generate_message() also re-checks this after
# the fact (see _has_third_person_rajat()) and retries once if it slips through.
_PERSONA_INSTRUCTION = (
    "You are Rajat, a GTM Engineer, personally writing a short LinkedIn outreach "
    "message. Write it in first person, as yourself. Never refer to yourself as "
    '"Rajat" in the third person (no "Rajat works...", "he could...", "his '
    'team...", or similar).'
)
_PERSONA_BULLET = (
    '- Written entirely in first person as yourself — never "Rajat works," '
    '"he could," "he\'s," "his team," or similar third-person phrasing'
)

# Bare fallback: no fetched job description, no research. Used only when both
# are unavailable (see generate_message()).
MESSAGE_PROMPT = _PERSONA_INSTRUCTION + """

Context:
- Founder name: {founder_name}
- Company: {company_name}
- YC Batch: {yc_batch}
- Role they are hiring for: {role_title}
- Product summary: {product_summary}

Write a single LinkedIn message that:
- Opens by referencing the specific role they are hiring for
- Mentions that you currently work with a few YC startups building their GTM infrastructure from scratch including signal systems, automated sequences, and GTM agents
- Says you could have something running in a week
- Ends with a soft yes or no ask
- Sounds like a real person wrote it, not a template
""" + _PERSONA_BULLET + """
- No em dashes, no ampersands, no special characters that LinkedIn might mangle
- Maximum 4 sentences
- Do not use the words seamless, robust, leverage, streamline, innovative, or comprehensive"""

# Used when a job description was fetched but no verified, founder-specific
# research exists for this founder (not attempted, or attempted and nothing
# survived verification). Explicitly told NOT to imply personal knowledge of the
# founder it doesn't have — that was bug #2: every hook was JD-only dressed up
# as if the (supposedly added) research had grounded it.
MESSAGE_PROMPT_JD_ONLY = _PERSONA_INSTRUCTION + """

Context:
- Founder name: {founder_name}
- Company: {company_name}
- YC Batch: {yc_batch}
- Role they are hiring for: {role_title}
- Product summary: {product_summary}
- Actual job description, scraped from the posting itself:
{jd_text}

You do NOT have any verified research on this specific founder — no confirmed public activity,
background, or statements about them personally. Do not imply or claim any personal knowledge of
the founder beyond what's in the job description above.

Write a single LinkedIn message that:
- Opens by referencing one specific, real detail from the job description above
- Mentions that you currently work with a few YC startups building their GTM infrastructure from scratch including signal systems, automated sequences, and GTM agents
- Says you could have something running in a week
- Ends with a soft yes or no ask
- Sounds like a real person wrote it, not a template
""" + _PERSONA_BULLET + """
- No em dashes, no ampersands, no special characters that LinkedIn might mangle
- Maximum 4 sentences
- Grounds its opening only in the job description above — never infer or invent a founder-specific detail you don't actually have
- Do not use the words seamless, robust, leverage, streamline, innovative, or comprehensive"""

# Used only when there's at least one verified, founder-specific research fact
# (see generate_message()) — this is the whole reason the research pipeline
# exists, so the opening hook is required to use it, not just the JD.
MESSAGE_PROMPT_RESEARCHED = _PERSONA_INSTRUCTION + """

Context:
- Founder name: {founder_name}
- Company: {company_name}
- YC Batch: {yc_batch}
- Role they are hiring for: {role_title}
- Product summary: {product_summary}
- Actual job description, scraped from the posting itself:
{jd_text}
- Verified research about this specific founder/company (already fact-checked for recency and that
  it actually describes what it claims to — only reference items listed here, nothing else):
{research_block}

Write a single LinkedIn message that:
- Opens by referencing one specific, founder-specific detail from the verified research above — not a generic line from the job description. This is the entire point of the research: use it.
- Mentions that you currently work with a few YC startups building their GTM infrastructure from scratch including signal systems, automated sequences, and GTM agents
- Says you could have something running in a week
- Ends with a soft yes or no ask
- Sounds like a real person wrote it, not a template
""" + _PERSONA_BULLET + """
- No em dashes, no ampersands, no special characters that LinkedIn might mangle
- Maximum 5 sentences
- Grounds every specific claim in the job description or verified research given above — never infer or invent a detail that isn't actually there
- Do not use the words seamless, robust, leverage, streamline, innovative, or comprehensive"""

_PERSONA_RETRY_SUFFIX = (
    "\n\nIMPORTANT: your previous draft referred to Rajat in the third person. "
    'Rewrite this from scratch as Rajat writing in first person only — use "I", '
    'never "Rajat works", "he could", "he\'s", "his team", or similar third-person '
    "phrasing anywhere in the message."
)

# Catches the exact failure mode seen in production: some drafts were written as
# Rajat ("I'm Rajat..."), others talked about Rajat in the third person ("Rajat
# works with...", "he could have something running...") in the same run, even
# for two founders at the same company. Deliberately narrow (verb-anchored, not
# a bare "Rajat" or "he" match) so a natural first-person opener like "I'm Rajat,
# a GTM Engineer" never false-positives.
_THIRD_PERSON_RAJAT_PATTERN = re.compile(
    r"\bRajat\s+(?:works|is|has|currently|builds|runs|does|could|would|will|can)\b"
    r"|\bhe(?:'s|\s+(?:is|works|could|would|will|can|currently|builds|runs|does))\b"
    r"|\bhis\s+(?:GTM|team|clients|work|company|infrastructure)\b",
    re.IGNORECASE,
)


def _has_third_person_rajat(message: str) -> bool:
    return bool(_THIRD_PERSON_RAJAT_PATTERN.search(message or ""))


# Production reasoning strings before this fix show exactly the failure mode
# this prompt now forbids: "listed first, likely CEO", "alphabetically first
# with no differentiating info", "a common choice when no bio data
# distinguishes founders" — every one of those is list/alphabetical order
# dressed up as a finding. The old prompt's own "default to whichever founder
# has the most senior title" fallback is what produced them: told to always
# pick someone, the model rationalized a pick even with nothing to go on.
BEST_FOUNDER_PROMPT = """You are helping a GTM Engineer named Rajat decide which co-founder to
reach out to first about a "{role_title}" role at {company_name}.

Here are the co-founders, with their title and bio from YC's site, in the order they appear on
the page:

{founders_block}

Pick the ONE founder who is the best fit to reach out to about this specific role, but ONLY if
there is an actual signal for it: an explicit sales, GTM, growth, or commercial background in
their bio, a CEO/COO/commercial title, or something in the role title or bios that clearly points
at them specifically over the others.

The order founders are listed in above is NOT a signal — it is just the order YC's page happens to
render them in. Do not use "listed first", alphabetical order, seniority guessed from title alone
with no other evidence, or "the common/default choice" as your reason. If the bios are empty,
generic, or don't meaningfully distinguish the founders from each other, that means there is no
signal — say so honestly rather than manufacturing a reason to pick one.

Respond with ONLY valid JSON in this exact format, no other text before or after it:
{{"founder": "<exact name as listed above, or empty string if no real signal exists>", "reason": "<one sentence, no more than 20 words, naming the actual signal — or 'no distinguishing signal' if none>"}}"""

# One extra call at the end of a run, only when there's enough in the digest to
# actually triage (see MIN_ROLES_FOR_RANKING) — without this, a 10-company,
# 20-founder digest presents every lead as equally worth acting on right now.
RANK_LEADS_PROMPT = """You are helping a GTM Engineer named Rajat triage {n} newly posted GTM/sales
roles at YC startups into what's worth same-day outreach versus what can wait.

Leads:
{leads_block}

Pick the 2 to 3 leads most worth acting on today. Weigh things like: a role that reads urgent or
foundational (e.g. "Founding AE" or first sales/growth hire, versus a lower-signal title), a
company with a genuinely confirmed best-fit founder (not "no distinguishing signal"), and any
verified research signal available for that founder. Do not simply pick the first N leads in the
list — judge each one on these merits.

Respond with ONLY valid JSON in this exact format, no other text before or after it:
{{"top_picks": [{{"role_url": "<exact role_url from above>", "reason": "<one sentence, max 20 words, on why this one>"}}]}}
Return between 2 and 3 items — fewer only if there genuinely aren't that many distinct leads."""


# ── Hunter email enrichment ───────────────────────────────────────────────────

def _company_domain(website: str) -> str:
    """Strip a company website URL down to its bare domain."""
    if not website:
        return ""
    parsed = urlparse(website if "//" in website else f"https://{website}")
    host = parsed.hostname or ""
    return host.removeprefix("www.")


# Matches a real observed bug: a YC page emitted a founder LinkedIn href as
# "https:https://www.linkedin.com/in/mbrady4/" — a doubled scheme. Cheap to
# happen again on some other page, so this is applied both at the scrape source
# (_parse_founders) and again as a last-mile check in validate_entries() right
# before the digest sends.
_DOUBLE_SCHEME_RE = re.compile(r"^https?:(https?://.+)$", re.IGNORECASE)


def _clean_url(url: str) -> str:
    """Normalizes a scraped/external URL and drops it (returns '') rather than
    let a malformed one reach the digest."""
    url = (url or "").strip()
    if not url:
        return ""
    m = _DOUBLE_SCHEME_RE.match(url)
    if m:
        url = m.group(1)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def find_email(domain: str, first_name: str, last_name: str) -> tuple[str, int]:
    """Return (email, confidence_score) from Hunter's email-finder API.
    Returns ('', 0) when the key is unset, the domain is empty, or the lookup fails."""
    if not HUNTER_API_KEY or not domain or not first_name or not last_name:
        return "", 0
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/email-finder",
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": HUNTER_API_KEY,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return "", 0
        data = resp.json().get("data", {})
        email = data.get("email") or ""
        score = int(data.get("score") or 0)
        return email, score
    except Exception:
        return "", 0


# ── Step 1: Pull companies from YC API ────────────────────────────────────────

def fetch_all_companies() -> list[dict]:
    print("[1] Fetching all companies from YC API …")
    companies: list[dict] = []
    url = f"{YC_API_BASE}?page=1&per_page=100"
    page_num = 1

    while url:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=PAGE_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ERROR fetching page {page_num}: {e} — stopping pagination")
            break

        batch = data.get("companies", [])
        companies.extend(batch)

        total_pages = data.get("totalPages", page_num)
        if page_num % 20 == 0 or page_num == total_pages:
            print(f"  Page {page_num}/{total_pages} — {len(companies)} companies so far")

        url = data.get("nextPage")
        page_num += 1
        time.sleep(API_SLEEP)

    print(f"  Total companies pulled: {len(companies)}")
    return companies


# ── Step 1b: Scrape each company's YC page for jobs + founders ───────────────

def _parse_jobs(soup: BeautifulSoup, slug: str) -> list[dict]:
    """Return [{title, url}] for GTM-relevant roles on a company page."""
    jobs = []
    seen_titles = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/companies/" in href and "/jobs/" in href:
            title = a.get_text(strip=True)
            if not title or title in seen_titles:
                continue
            if not GTM_PATTERN.search(title):
                continue
            seen_titles.add(title)
            full_url = href if href.startswith("http") else f"https://www.ycombinator.com{href}"
            jobs.append({"title": title, "url": full_url})
    return jobs


def _parse_founders(soup: BeautifulSoup) -> list[dict]:
    """Parse founder cards (name, title, linkedin) from a YC company page."""
    founders = []
    seen_names = set()

    for a in soup.find_all("a", attrs={"aria-label": "LinkedIn profile"}):
        linkedin = _clean_url(a.get("href", ""))
        if linkedin and "/company/" in linkedin:
            continue

        card = a
        for _ in range(6):
            card = card.parent
            if not card:
                break
            if card.find(class_=re.compile(r"text-xl")):
                break
        if not card:
            continue

        name_el = card.find(class_=re.compile(r"text-xl"))
        title_el = card.find(class_=re.compile(r"text-gray-600"))
        bio_el = card.find(class_=re.compile(r"prose"))
        name = name_el.get_text(strip=True) if name_el else ""
        title = title_el.get_text(strip=True) if title_el else "Founder"
        bio = bio_el.get_text(" ", strip=True) if bio_el else ""

        if not name or name in seen_names or not re.search(r" ", name):
            continue

        seen_names.add(name)
        founders.append({"name": name, "title": title, "linkedin": linkedin, "bio": bio})

    # Fallback: founders without a LinkedIn link, under "Active Founders"
    for heading in soup.find_all(string=re.compile(r"Active Founders", re.I)):
        section = heading.parent
        for _ in range(5):
            if not section:
                break
            section = section.parent
            name_els = section.find_all(class_=re.compile(r"text-xl"))
            for ne in name_els:
                name = ne.get_text(strip=True)
                if name and name not in seen_names and len(name) > 2:
                    p = ne.parent
                    title_el = p.find(class_=re.compile(r"text-gray-600")) if p else None
                    title = title_el.get_text(strip=True) if title_el else "Founder"
                    seen_names.add(name)
                    founders.append({"name": name, "title": title, "linkedin": "", "bio": ""})
            if founders:
                break

    return founders


def scrape_company(company: dict) -> tuple[str, list[dict], list[dict]]:
    """Returns (slug, gtm_jobs, founders). Empty lists on any failure/timeout."""
    slug = company.get("slug", "")
    yc_url = company.get("url") or f"https://www.ycombinator.com/companies/{slug}"
    try:
        resp = requests.get(yc_url, headers=HEADERS, timeout=SITE_TIMEOUT)
        if resp.status_code != 200:
            return slug, [], []
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return slug, [], []

    jobs = _parse_jobs(soup, slug)
    founders = _parse_founders(soup) if jobs else []
    return slug, jobs, founders


def scrape_all_companies(companies: list[dict]) -> dict[str, dict]:
    """Returns slug -> {"jobs": [...], "founders": [...]} for companies with GTM roles."""
    print(f"\n[2] Scraping {len(companies)} company pages for GTM roles + founders …")
    results: dict[str, dict] = {}
    done = 0
    skipped = 0
    total = len(companies)

    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as ex:
        futures = {ex.submit(scrape_company, c): c for c in companies}
        for fut in as_completed(futures):
            done += 1
            c = futures[fut]
            try:
                slug, jobs, founders = fut.result()
                if jobs:
                    results[slug] = {"jobs": jobs, "founders": founders}
            except Exception as e:
                skipped += 1
                print(f"  SKIPPED {c.get('name', '?')}: {e}")
            if done % 200 == 0 or done == total:
                print(f"  [{done}/{total}] pages scraped … ({len(results)} companies with GTM roles)")

    print(f"  Companies with GTM-relevant roles: {len(results)} | Skipped (timeout/error): {skipped}")
    return results


# ── Step 1c: Fetch job descriptions for new roles ─────────────────────────────

def fetch_job_description(url: str) -> str:
    """Fetch a single job's YC page and pull the actual description text
    (requirements, responsibilities, the specific problem they're hiring to solve) —
    the index-page scrape in _parse_jobs() only ever sees the title. Returns ''
    on any failure/timeout so callers degrade gracefully instead of crashing."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=SITE_TIMEOUT)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return ""

    # YC renders rich text (founder bios, company descriptions, job descriptions)
    # in "prose" typography containers — same pattern _parse_founders() already
    # relies on for bio text. A job page's description is the largest one on it.
    blocks = soup.find_all(class_=re.compile(r"prose"))
    if not blocks:
        return ""
    body = max(blocks, key=lambda b: len(b.get_text(strip=True)))
    text = body.get_text("\n", strip=True)
    return text[:JD_MAX_CHARS]


def fetch_job_descriptions(jobs: list[dict]) -> None:
    """Fills in job["jd_text"] in place for each job, parallelized like the
    company-page scrape. Only called on already-diffed new roles, so volume is
    small (typically a handful a day) even though this hits one URL per job."""
    if not jobs:
        return
    print(f"\n[3b] Fetching job descriptions for {len(jobs)} new role(s) …")
    with ThreadPoolExecutor(max_workers=min(SCRAPE_WORKERS, len(jobs))) as ex:
        futures = {ex.submit(fetch_job_description, job["url"]): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                job["jd_text"] = fut.result()
            except Exception:
                job["jd_text"] = ""


# ── Step 2: Diff against seen_jobs.json ───────────────────────────────────────

def job_key(url: str) -> str:
    """The job's URL is the canonical identity for a posting — unlike a
    company+title pair, it's unique even when two roles share a title, and
    stable even if a company edits a role's title later."""
    return url


def legacy_job_key(company_name: str, role_title: str) -> str:
    """Old key format (pre-URL-based matching). Used only to migrate
    existing seen_jobs.json entries onto the new key without re-flagging
    everything already tracked as 'new'."""
    return f"{company_name}::{role_title}"


def load_seen() -> dict:
    if SEEN_JOBS_PATH.exists():
        try:
            return json.loads(SEEN_JOBS_PATH.read_text())
        except Exception as e:
            print(f"  WARNING: could not parse {SEEN_JOBS_PATH}, treating as empty: {e}")
            return {}
    return {}


def save_seen(seen: dict):
    SEEN_JOBS_PATH.write_text(json.dumps(seen, indent=2))


# ── Step 3a: Exa research (best-fit founder only) + fact verification ────────
#
# Scoped deliberately to the single founder pick_best_founder() flags as the
# best fit — not every founder at a multi-founder company. seen_jobs.json diffs
# on a normal day run to a handful of new roles (single digits; a multi-day gap
# can produce a few dozen), so even at 6 Exa calls + up to 6 verification calls
# per company this stays well within a predictable daily budget. Companies with
# no founders, or no clear best fit, never reach this code path at all — see
# build_entries() for the gate.

def exa_search(query: str, num_results: int = EXA_RESULTS_PER_QUERY) -> list[dict]:
    """Runs one Exa search call. Returns [{url, title, published_date, author, text}],
    or [] on a missing key, timeout, or any other failure — callers must treat
    that as 'nothing found', not an error to propagate."""
    if not EXA_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": num_results,
                "contents": {"text": {"maxCharacters": EXA_TEXT_MAX_CHARS}},
            },
            timeout=EXA_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
    except Exception:
        return []

    return [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "published_date": r.get("publishedDate", ""),
            "author": r.get("author", ""),
            "text": (r.get("text") or "")[:EXA_TEXT_MAX_CHARS],
        }
        for r in results
    ]


def research_founder(founder_name: str, company_name: str, product_summary: str, jd_text: str) -> list[dict]:
    """Runs a bounded set of Exa searches (2 founder-activity, 2 competitor, 2
    account-signal — 6 calls total) for one founder. Returns raw, UNVERIFIED
    candidate facts tagged with a category; [] on any failure."""
    if not EXA_API_KEY:
        return []

    candidates = []
    try:
        activity = exa_search(
            f'"{founder_name}" {company_name} LinkedIn post OR podcast OR conference talk OR Twitter'
        )
        for r in activity:
            r["category"] = "founder_activity"
        candidates.extend(activity)

        competitors = exa_search(f"competitors alternatives to {company_name} {product_summary}".strip())
        for r in competitors:
            r["category"] = "competitor"
        candidates.extend(competitors)

        signal_topic = (jd_text or product_summary or company_name)[:200]
        signal = exa_search(
            f"blog post OR GitHub issue OR changelog describing this problem: {signal_topic}"
        )
        for r in signal:
            r["category"] = "account_signal"
        candidates.extend(signal)
    except Exception as e:
        print(f"  WARNING: Exa research failed for {founder_name} @ {company_name}: {e}")
        return []

    return candidates


VERIFY_PROMPT = """You are fact-checking one research source before it gets used in a sales
outreach message. Today's date is {today}.

Source title: {title}
Source URL: {url}
Source published/byline date as reported by the search API: {published_date}
What this source is being cited for: {category}
Source text excerpt:
{text}

Check exactly two things:
1. Recency: does the source's actual byline or publish date genuinely fall within roughly the
   last 12 months of today's date? Judge from the substance of the text too, not just the date
   field — a generic "updated" timestamp is sometimes stamped over much older content, and that
   should fail this check.
2. Sentiment match: for a founder_activity or account_signal source, does the text actually
   describe a live pain point, open problem, or current thesis — and NOT a success story, a
   "we solved this" / "we scaled fine" resolution, or something unrelated? For a competitor
   source, does it actually describe a competing product's positioning?

Respond with ONLY valid JSON, no other text before or after it:
{{"verified": true or false, "reason": "<one sentence, max 25 words, explaining why>"}}"""


def verify_fact(client, fact: dict) -> dict:
    """Runs one cheap Claude call checking a single Exa result for recency and
    sentiment match. Mutates and returns fact with 'verified' (bool) and
    'verify_reason' (str) added. A failed/unparseable call marks it unverified —
    never pass an unchecked claim into the message prompt.

    thinking=THINKING_DISABLED is load-bearing here, not decoration: without it,
    every single verification call in production came back with content[0].text
    stripped to "" (json.loads raised "Expecting value: line 1 column 1 (char
    0)" every time, on every fact, for days) because VERIFY_MODEL defaults to
    adaptive extended thinking when the param is omitted, and the whole 150-token
    budget was being consumed by invisible reasoning before any visible answer
    token got emitted. Confirmed by isolating it from generate_message()'s and
    pick_best_founder()'s calls (same SDK, same account, different model,
    zero failures) — this wasn't a rate limit or auth issue, see the explicit
    stop_reason/empty-content diagnostic below for how to tell if it recurs."""
    prompt = VERIFY_PROMPT.format(
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        title=fact.get("title", ""),
        url=fact.get("url", ""),
        published_date=fact.get("published_date") or "unknown",
        category=fact.get("category", ""),
        text=fact.get("text", "")[:1500],
    )
    try:
        msg = client.messages.create(
            model=VERIFY_MODEL,
            max_tokens=200,
            thinking=THINKING_DISABLED,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = msg.content[0].text.strip() if msg.content else ""
        if not raw_text:
            raise ValueError(
                f"empty response text (stop_reason={getattr(msg, 'stop_reason', '?')})"
            )
        data = json.loads(raw_text)
        fact["verified"] = bool(data.get("verified", False))
        fact["verify_reason"] = data.get("reason", "")
    except Exception as e:
        fact["verified"] = False
        fact["verify_reason"] = f"verification call failed: {e}"
    return fact


def research_and_verify_founder(client, founder_name: str, company_name: str,
                                 product_summary: str, jd_text: str) -> list[dict]:
    """Full research pipeline for one best-fit founder: bounded Exa search, then
    a verification pass per candidate fact, returning only facts that passed.
    Returns [] (never raises) on any failure so callers fall back gracefully to
    the lighter-weight message instead of losing the entry."""
    try:
        candidates = research_founder(founder_name, company_name, product_summary, jd_text)
    except Exception as e:
        print(f"  WARNING: research step failed for {founder_name} @ {company_name}: {e}")
        return []

    verified = []
    for fact in candidates:
        checked = verify_fact(client, fact)
        if checked["verified"]:
            verified.append(checked)
        else:
            print(f"    Discarded unverified {checked.get('category')} fact: {checked.get('verify_reason')}")
    return verified


def _format_research_block(research: list[dict]) -> str:
    if not research:
        return "None verified — write from the job description and product summary alone."
    labels = {
        "founder_activity": "Founder activity",
        "competitor": "Competitor",
        "account_signal": "Account signal",
    }
    lines = []
    for r in research:
        label = labels.get(r.get("category", ""), "Research")
        excerpt = r.get("text", "")[:300]
        lines.append(f"- [{label}] {r.get('title', '')}: {excerpt} (source: {r.get('url', '')})")
    return "\n".join(lines)


# ── Step 3 + 4: Founder enrichment + message generation ──────────────────────

def generate_message(client, founder_name: str, company_name: str, yc_batch: str,
                      role_title: str, product_summary: str,
                      jd_text: str = "", research: list[dict] | None = None) -> tuple[str, str]:
    """Returns (message, message_basis), message_basis one of "researched" (at
    least one verified, founder-specific fact — the whole point of the research
    pipeline, so this is the only tier allowed to claim founder-specific
    knowledge), "jd_only" (a fetched job description but no verified research —
    explicitly told not to imply personal knowledge it doesn't have), or "plain"
    (neither available, the original one-liner-based prompt).

    Re-checks its own output for third-person Rajat language and retries once
    with a corrective prompt if it slips through — this was bug #1 in
    production: the same run wrote one founder's message as Rajat and another's
    about Rajat. Still lets a bad draft through after the retry rather than
    dropping the entry; validate_entries() flags it in the digest as a final
    safety net so it's never silently sent."""
    research = research or []
    if research:
        prompt = MESSAGE_PROMPT_RESEARCHED.format(
            founder_name=founder_name,
            company_name=company_name,
            yc_batch=yc_batch,
            role_title=role_title,
            product_summary=product_summary or "Not available",
            jd_text=(jd_text[:JD_MAX_CHARS] if jd_text else "Not available"),
            research_block=_format_research_block(research),
        )
        message_basis = "researched"
        max_tokens = 400
    elif jd_text:
        prompt = MESSAGE_PROMPT_JD_ONLY.format(
            founder_name=founder_name,
            company_name=company_name,
            yc_batch=yc_batch,
            role_title=role_title,
            product_summary=product_summary or "Not available",
            jd_text=jd_text[:JD_MAX_CHARS],
        )
        message_basis = "jd_only"
        max_tokens = 350
    else:
        prompt = MESSAGE_PROMPT.format(
            founder_name=founder_name,
            company_name=company_name,
            yc_batch=yc_batch,
            role_title=role_title,
            product_summary=product_summary or "Not available",
        )
        message_basis = "plain"
        max_tokens = 350

    text = ""
    for attempt in range(2):
        try:
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                thinking=THINKING_DISABLED,
                messages=[{
                    "role": "user",
                    "content": prompt + (_PERSONA_RETRY_SUFFIX if attempt else ""),
                }],
            )
            text = msg.content[0].text.strip() if msg.content else ""
        except Exception as e:
            print(f"  ERROR generating message for {founder_name} @ {company_name}: {e}")
            return "", message_basis
        if text and not _has_third_person_rajat(text):
            return text, message_basis
        if attempt == 0:
            print(f"    Draft for {founder_name} @ {company_name} used third-person "
                  f"Rajat language, retrying …")
    print(f"    WARNING: persona check still failing after retry for {founder_name} "
          f"@ {company_name} — flagging for manual review")
    return text, message_basis


def pick_best_founder(client, founders: list[dict], company_name: str, role_title: str) -> tuple[str, str]:
    """Returns (founder_name, one_sentence_reason). founder_name is '' both when
    the call fails/can't parse AND when Claude itself reports no distinguishing
    signal among the founders — callers must treat both the same way ('no
    recommendation'), not just the failure case. See BEST_FOUNDER_PROMPT: it's
    explicitly told not to default to list order or a title guess when the
    bios don't actually distinguish the founders, since that's what produced
    reasoning like "listed first, likely CEO" in production — a default
    dressed up as a finding, not a real signal."""
    founders_block = "\n".join(
        f"- {f.get('name', 'Unknown')} ({f.get('title', 'Founder')}): {f.get('bio') or 'no bio available'}"
        for f in founders
    )
    prompt = BEST_FOUNDER_PROMPT.format(
        role_title=role_title, company_name=company_name, founders_block=founders_block
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            thinking=THINKING_DISABLED,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = msg.content[0].text.strip() if msg.content else ""
        if not raw_text:
            raise ValueError(
                f"empty response text (stop_reason={getattr(msg, 'stop_reason', '?')})"
            )
        data = json.loads(raw_text)
        return data.get("founder", ""), data.get("reason", "")
    except Exception as e:
        print(f"  WARNING: could not determine best-fit founder for {company_name}: {e}")
        return "", ""


def build_entries(new_jobs: list[dict], scraped: dict[str, dict], client) -> list[dict]:
    """One entry per (new role, founder). Companies with no founders get one placeholder entry.

    Deep Exa research only ever runs for the founder flagged as best-fit — a
    single, unambiguous founder (the only one, if there's just one) or the one
    pick_best_founder() names among several. Companies with multiple founders and
    no clear best fit get no research and the plain one-liner-based message,
    same as before this change. research_cache means a company with more than
    one new GTM role on the same day only gets researched once, not once per role.
    """
    print(f"\n[4] Enriching {len(new_jobs)} new role(s) with founder data + generating messages …")
    entries = []
    research_cache: dict[str, list[dict]] = {}

    for job in new_jobs:
        slug = job["slug"]
        company_name = job["company_name"]
        yc_batch = job["yc_batch"]
        role_title = job["title"]
        role_url = job["url"]
        product_summary = job["product_summary"]
        jd_text = job.get("jd_text", "")
        domain = _company_domain(job.get("website", ""))

        founders = scraped.get(slug, {}).get("founders", [])

        if not founders:
            print(f"  No founder data found for {company_name} — including role without enrichment")
            entries.append({
                "company_name": company_name,
                "yc_batch": yc_batch,
                "role_title": role_title,
                "role_url": role_url,
                "founder_name": "Unknown",
                "linkedin_url": "",
                "email_address": "",
                "email_confidence": 0,
                "product_summary": product_summary,
                "message": "(no founder data found on YC page)",
                "message_basis": "none",
                "is_best_fit": False,
                "best_fit_reason": "",
                "research_attempted": False,
                "research_verified_count": 0,
            })
            continue

        best_founder_name, best_fit_reason = "", ""
        if len(founders) == 1:
            # A single founder is an unambiguous best fit — no Claude call needed
            # to "decide", and this is the case the deep-research gate is meant
            # to include, not just multi-founder companies with a clear pick.
            best_founder_name = founders[0].get("name", "")
            best_fit_reason = "Only founder listed on the company's YC page"
        elif len(founders) > 1:
            best_founder_name, best_fit_reason = pick_best_founder(
                client, founders, company_name, role_title
            )
            if best_founder_name:
                print(f"  Best-fit founder for {company_name}: {best_founder_name} — {best_fit_reason}")

        research_attempted = False
        best_fit_research: list[dict] = []
        if best_founder_name and EXA_API_KEY:
            if slug not in research_cache:
                print(f"  Researching best-fit founder {best_founder_name} @ {company_name} …")
                try:
                    research_cache[slug] = research_and_verify_founder(
                        client, best_founder_name, company_name, product_summary, jd_text
                    )
                    print(f"    {len(research_cache[slug])} fact(s) verified")
                except Exception as e:
                    # research_founder()/verify_fact() already catch their own
                    # network/parsing failures internally — this guards against
                    # anything unexpected escaping that, so one bad entry can
                    # never take down the whole run. Falls back to the lighter
                    # jd-only/plain message below instead of losing the entry.
                    print(f"    WARNING: research pipeline failed unexpectedly, falling back: {e}")
                    research_cache[slug] = []
            best_fit_research = research_cache[slug]
            research_attempted = True

        for f in founders:
            founder_name = f.get("name", "Unknown")
            print(f"  Generating message: {founder_name} @ {company_name} — {role_title}")
            is_best_fit = bool(best_founder_name) and founder_name == best_founder_name
            research_for_founder = best_fit_research if is_best_fit else []

            message, message_basis = generate_message(
                client, founder_name, company_name, yc_batch, role_title, product_summary,
                jd_text=jd_text, research=research_for_founder,
            )

            name_parts = founder_name.split(None, 1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            email_addr, email_score = find_email(domain, first_name, last_name)
            if email_addr and email_score >= HUNTER_MIN_CONFIDENCE:
                print(f"    Email found: {email_addr} ({email_score}% confidence)")

            entries.append({
                "company_name": company_name,
                "yc_batch": yc_batch,
                "role_title": role_title,
                "role_url": role_url,
                "founder_name": founder_name,
                "linkedin_url": f.get("linkedin", ""),
                "email_address": email_addr if email_score >= HUNTER_MIN_CONFIDENCE else "",
                "email_confidence": email_score if email_score >= HUNTER_MIN_CONFIDENCE else 0,
                "product_summary": product_summary,
                "is_best_fit": is_best_fit,
                "best_fit_reason": best_fit_reason if is_best_fit else "",
                "message": message,
                "message_basis": message_basis,
                "research_attempted": is_best_fit and research_attempted,
                "research_verified_count": len(research_for_founder),
            })

    print(f"  Built {len(entries)} outreach entries")
    return entries


# ── Step 5: Email digest ──────────────────────────────────────────────────────

_MESSAGE_BASIS_LABELS = {
    "researched": "founder-specific research ({count} fact(s) verified)",
    "jd_only": "job description only — no verified founder-specific research",
    "plain": "general blurb only — job description unavailable",
    "none": "no founder data on the YC page",
}


def format_entry(e: dict) -> str:
    founder_line = f"Founder: {e['founder_name']}"
    if e.get("is_best_fit"):
        founder_line += "  [BEST FIT]"
    if e.get("is_top_pick"):
        founder_line += "  [TOP PICK]"

    lines = [
        f"Company: {e['company_name']} ({e['yc_batch']})",
        f"Role: {e['role_title']}",
        f"Job: {e['role_url']}",
        founder_line,
    ]
    if e.get("is_best_fit") and e.get("best_fit_reason"):
        lines.append(f"Why: {e['best_fit_reason']}")
    if e.get("is_top_pick") and e.get("top_pick_reason"):
        lines.append(f"Top pick because: {e['top_pick_reason']}")
    lines.append(f"LinkedIn: {e['linkedin_url']}")
    if e.get("email_address"):
        lines.append(f"Email: {e['email_address']} ({e['email_confidence']}% confidence)")
    # Bug #2: every message used to look founder-personalized whether or not it
    # actually was. This line states plainly what the message is actually
    # grounded in, so "researched" can be trusted and "jd_only" isn't mistaken
    # for it.
    basis = e.get("message_basis", "plain")
    label = _MESSAGE_BASIS_LABELS.get(basis, basis).format(
        count=e.get("research_verified_count", 0)
    )
    lines.append(f"Message basis: {label}")
    if e.get("persona_check_failed"):
        lines.append("⚠ PERSONA CHECK FAILED — draft still reads third-person after a retry, rewrite before sending")
    lines.append(f"Product: {e['product_summary']}")
    lines.append("")
    lines.append(f"Message:\n{e['message']}")

    return "\n".join(lines) + "\n"


def rank_top_leads(entries: list[dict], client) -> dict[str, str]:
    """Ranks distinct (company, role) leads across the WHOLE digest — not just
    within a company — and returns {role_url: reason} for the 2-3 most worth
    same-day action. Returns {} (never raises) when there aren't enough
    distinct leads to bother triaging (below MIN_ROLES_FOR_RANKING) or the call
    fails; every entry just shows unelevated in that case, same as before this
    existed. Doesn't remove or downrank anything — a 10-company, 20-founder
    digest presenting every lead as equally worth acting on today was bug #4;
    this only adds a flag on top of the top 2-3."""
    by_role: dict[str, dict] = {}
    for e in entries:
        by_role.setdefault(e["role_url"], e)

    if len(by_role) < MIN_ROLES_FOR_RANKING:
        return {}

    print(f"\n[4b] Ranking {len(by_role)} leads across the digest for same-day triage …")
    leads_block = "\n".join(
        f"- role_url: {url}\n"
        f"  Company: {e['company_name']} ({e['yc_batch']}) — Role: {e['role_title']}\n"
        f"  Best-fit founder: "
        + (f"{e['founder_name']}" + (f" — {e['best_fit_reason']}" if e.get("best_fit_reason") else "")
           if e.get("is_best_fit") else "no distinguishing signal")
        + f"\n  Research: {e.get('research_verified_count', 0)} verified fact(s)\n"
          f"  Product: {e['product_summary']}"
        for url, e in by_role.items()
    )
    prompt = RANK_LEADS_PROMPT.format(n=len(by_role), leads_block=leads_block)
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            thinking=THINKING_DISABLED,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = msg.content[0].text.strip() if msg.content else ""
        if not raw_text:
            raise ValueError(
                f"empty response text (stop_reason={getattr(msg, 'stop_reason', '?')})"
            )
        data = json.loads(raw_text)
        picks = {}
        for item in data.get("top_picks", [])[:3]:
            url = item.get("role_url", "")
            if url in by_role:
                picks[url] = item.get("reason", "")
        print(f"    {len(picks)} top pick(s) flagged")
        return picks
    except Exception as e:
        print(f"  WARNING: lead ranking failed, digest will show every entry unelevated: {e}")
        return {}


def validate_entries(entries: list[dict]) -> list[dict]:
    """Last-mile safety net run right before the digest sends: re-sanitizes URL
    fields (bug #5 — a YC page emitted a doubled-scheme LinkedIn href,
    'https:https://...') and flags any message that still contains
    third-person Rajat language even after generate_message()'s own retry
    (bug #1), rather than silently shipping either. Mutates and returns
    entries."""
    for e in entries:
        e["linkedin_url"] = _clean_url(e.get("linkedin_url", ""))
        cleaned_role_url = _clean_url(e.get("role_url", ""))
        if cleaned_role_url:
            e["role_url"] = cleaned_role_url
        e["persona_check_failed"] = bool(
            e.get("message") and _has_third_person_rajat(e["message"])
        )
    return entries


def send_email(entries: list[dict]) -> bool:
    """Returns True only if the email was actually delivered — callers use this
    to decide whether it's safe to mark these roles as seen.

    Sends via Gmail SMTP. The Railway deployment of this same script uses
    Resend's HTTP API instead because Railway blocks outbound SMTP entirely —
    GitHub Actions' hosted runners don't have that restriction, so plain
    Gmail SMTP works fine here without requiring anyone forking this repo to
    own a domain just to send themselves a digest email."""
    if not entries:
        print("\n[5] No new roles — skipping email")
        return False

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("\n[5] ERROR: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, or RECIPIENT_EMAIL not set — skipping email")
        return False

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    distinct_roles = len({e["role_url"] for e in entries})
    role_word = "role" if distinct_roles == 1 else "roles"
    subject = f"YC GTM Monitor - {distinct_roles} new {role_word} ({len(entries)} founders) - {date_str}"

    body_parts = []
    top_picks_by_role: dict[str, dict] = {}
    for e in entries:
        if e.get("is_top_pick"):
            top_picks_by_role.setdefault(e["role_url"], e)
    if top_picks_by_role:
        header = ["TOP PICKS TODAY — worth same-day action:", ""]
        for e in top_picks_by_role.values():
            header.append(f"- {e['company_name']} — {e['role_title']}: {e.get('top_pick_reason', '')}")
        header.append("")
        header.append("=" * 60)
        body_parts.append("\n".join(header))
    body_parts.append("\n---\n".join(format_entry(e) for e in entries))
    body = "\n\n".join(body_parts)

    print(f"\n[5] Sending email digest: {subject}")
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = RECIPIENT_EMAIL

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
        print("  Email sent successfully")
        return True
    except Exception as e:
        print(f"  ERROR sending email: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("YC GTM JOB MONITOR")
    print(f"Run time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    first_run = not SEEN_JOBS_PATH.exists()

    companies = fetch_all_companies()
    if not companies:
        print("ERROR: No companies fetched. Exiting.")
        sys.exit(1)

    slug_to_company = {c.get("slug", ""): c for c in companies}
    scraped = scrape_all_companies(companies)

    # Flatten into job records with company metadata attached
    all_jobs = []
    for slug, data in scraped.items():
        c = slug_to_company.get(slug, {})
        company_name = c.get("name", slug)
        yc_batch = c.get("batch", "")
        product_summary = c.get("oneLiner") or c.get("longDescription", "")
        website = c.get("website", "")
        for job in data["jobs"]:
            all_jobs.append({
                "slug": slug,
                "company_name": company_name,
                "yc_batch": yc_batch,
                "title": job["title"],
                "url": job["url"],
                "product_summary": product_summary,
                "website": website,
            })

    print(f"\n[3] Diffing {len(all_jobs)} GTM-relevant roles against seen_jobs.json …")
    seen = load_seen()

    # Migrate any entries still under the old company::title key onto the new
    # URL-based key, so switching key formats doesn't re-flag everything
    # already tracked as "new" on the next run.
    migrated = 0
    for job in all_jobs:
        legacy_key = legacy_job_key(job["company_name"], job["title"])
        new_key = job_key(job["url"])
        if legacy_key in seen and new_key not in seen:
            old_value = seen.pop(legacy_key)
            first_seen = old_value["first_seen"] if isinstance(old_value, dict) else old_value
            seen[new_key] = {
                "company": job["company_name"],
                "title": job["title"],
                "first_seen": first_seen,
            }
            migrated += 1
    if migrated:
        print(f"  Migrated {migrated} role(s) from the old company::title key to the new URL-based key")
        save_seen(seen)

    if first_run:
        print("  First run detected — populating baseline, no email will be sent")
        for job in all_jobs:
            key = job_key(job["url"])
            seen[key] = {
                "company": job["company_name"],
                "title": job["title"],
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
        save_seen(seen)
        print(f"  Baseline saved: {len(seen)} roles in {SEEN_JOBS_PATH}")
        print("\nDONE (baseline run)")
        return

    new_jobs = [job for job in all_jobs if job_key(job["url"]) not in seen]
    print(f"  New roles found: {len(new_jobs)}")

    if not new_jobs:
        print("\nDONE — no new roles today")
        return

    fetch_job_descriptions(new_jobs)

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set — cannot generate messages. Exiting.")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    entries = build_entries(new_jobs, scraped, client)

    top_picks = rank_top_leads(entries, client)
    for e in entries:
        e["is_top_pick"] = e["role_url"] in top_picks
        e["top_pick_reason"] = top_picks.get(e["role_url"], "")

    entries = validate_entries(entries)

    email_sent = send_email(entries)

    if not email_sent:
        print("\nDONE — email did not send, so today's new roles were NOT marked as seen")
        print("They'll be retried as 'new' on the next run instead of being silently lost.")
        return

    # Only mark today's new roles as seen now that they've actually been emailed.
    # Already-seen roles don't need re-marking.
    for job in new_jobs:
        key = job_key(job["url"])
        seen[key] = {
            "company": job["company_name"],
            "title": job["title"],
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }
    save_seen(seen)
    print(f"\nDONE — seen_jobs.json updated: {len(seen)} total roles tracked")


if __name__ == "__main__":
    main()
