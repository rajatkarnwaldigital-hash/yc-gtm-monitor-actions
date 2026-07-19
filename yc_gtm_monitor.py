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

JD_MAX_CHARS = 6000  # plenty for a job description; keeps the prompt bounded
EXA_TIMEOUT = 20
EXA_RESULTS_PER_QUERY = 2
EXA_TEXT_MAX_CHARS = 2000  # per-result excerpt handed to the verifier

MESSAGE_PROMPT = """You are writing a short LinkedIn outreach message on behalf of a GTM Engineer named Rajat.

Context:
- Founder name: {founder_name}
- Company: {company_name}
- YC Batch: {yc_batch}
- Role they are hiring for: {role_title}
- Product summary: {product_summary}

Write a single LinkedIn message that:
- Opens by referencing the specific role they are hiring for
- Mentions that Rajat currently works with a few YC startups building their GTM infrastructure from scratch including signal systems, automated sequences, and GTM agents
- Says he could have something running in a week
- Ends with a soft yes or no ask
- Sounds like a real person wrote it, not a template
- No em dashes, no ampersands, no special characters that LinkedIn might mangle
- Maximum 4 sentences
- Do not use the words seamless, robust, leverage, streamline, innovative, or comprehensive"""

# Used instead of MESSAGE_PROMPT whenever there's a fetched job description and/or
# at least one verified research fact to ground the message in. Falls back to
# MESSAGE_PROMPT when neither is available (see generate_message()).
MESSAGE_PROMPT_ENRICHED = """You are writing a short LinkedIn outreach message on behalf of a GTM Engineer named Rajat.

Context:
- Founder name: {founder_name}
- Company: {company_name}
- YC Batch: {yc_batch}
- Role they are hiring for: {role_title}
- Product summary: {product_summary}
- Actual job description, scraped from the posting itself:
{jd_text}
- Verified research about this founder/company (already fact-checked for recency and that it
  actually describes what it claims to — only reference items listed here, nothing else):
{research_block}

Write a single LinkedIn message that:
- Opens by referencing one specific, real detail: something concrete from the job description
  above, or one verified research fact if one is listed
- Mentions that Rajat currently works with a few YC startups building their GTM infrastructure from
  scratch including signal systems, automated sequences, and GTM agents
- Says he could have something running in a week
- Ends with a soft yes or no ask
- Sounds like a real person wrote it, not a template
- No em dashes, no ampersands, no special characters that LinkedIn might mangle
- Maximum 5 sentences
- Grounds every specific claim in the job description or verified research given above — never
  infer or invent a detail that isn't actually there
- Do not use the words seamless, robust, leverage, streamline, innovative, or comprehensive"""

BEST_FOUNDER_PROMPT = """You are helping a GTM Engineer named Rajat decide which co-founder to
reach out to first about a "{role_title}" role at {company_name}.

Here are the co-founders, with their title and bio from YC's site:

{founders_block}

Pick the ONE founder who is the best fit to reach out to about this specific role. A founder
with sales, GTM, growth, or commercial background, or a CEO/COO title, is usually a better fit
for a sales or growth hire than a deeply technical CTO or engineering-focused founder. If nothing
in the bios points clearly one way, default to whichever founder has the most senior or
commercial-sounding title.

Respond with ONLY valid JSON in this exact format, no other text before or after it:
{{"founder": "<exact name as listed above>", "reason": "<one sentence, no more than 20 words>"}}"""


# ── Hunter email enrichment ───────────────────────────────────────────────────

def _company_domain(website: str) -> str:
    """Strip a company website URL down to its bare domain."""
    if not website:
        return ""
    parsed = urlparse(website if "//" in website else f"https://{website}")
    host = parsed.hostname or ""
    return host.removeprefix("www.")


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
        linkedin = a.get("href", "").strip()
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
    never pass an unchecked claim into the message prompt."""
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
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(msg.content[0].text.strip())
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
                      jd_text: str = "", research: list[dict] | None = None) -> str:
    """Uses MESSAGE_PROMPT_ENRICHED whenever there's a fetched JD and/or verified
    research to ground the message in; degrades to the original one-liner-based
    MESSAGE_PROMPT when neither is available, rather than failing."""
    if jd_text or research:
        prompt = MESSAGE_PROMPT_ENRICHED.format(
            founder_name=founder_name,
            company_name=company_name,
            yc_batch=yc_batch,
            role_title=role_title,
            product_summary=product_summary or "Not available",
            jd_text=(jd_text[:JD_MAX_CHARS] if jd_text else "Not available"),
            research_block=_format_research_block(research or []),
        )
    else:
        prompt = MESSAGE_PROMPT.format(
            founder_name=founder_name,
            company_name=company_name,
            yc_batch=yc_batch,
            role_title=role_title,
            product_summary=product_summary or "Not available",
        )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ERROR generating message for {founder_name} @ {company_name}: {e}")
        return ""


def pick_best_founder(client, founders: list[dict], company_name: str, role_title: str) -> tuple[str, str]:
    """Returns (founder_name, one_sentence_reason). Empty strings if it can't decide
    or the call fails — callers should treat that as 'no recommendation', not crash."""
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
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(msg.content[0].text.strip())
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

            message = generate_message(
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
                "research_attempted": is_best_fit and research_attempted,
                "research_verified_count": len(research_for_founder),
            })

    print(f"  Built {len(entries)} outreach entries")
    return entries


# ── Step 5: Email digest ──────────────────────────────────────────────────────

def format_entry(e: dict) -> str:
    founder_line = f"Founder: {e['founder_name']}"
    if e.get("is_best_fit"):
        founder_line += "  [BEST FIT]"

    lines = [
        f"Company: {e['company_name']} ({e['yc_batch']})",
        f"Role: {e['role_title']}",
        f"Job: {e['role_url']}",
        founder_line,
    ]
    if e.get("is_best_fit") and e.get("best_fit_reason"):
        lines.append(f"Why: {e['best_fit_reason']}")
    lines.append(f"LinkedIn: {e['linkedin_url']}")
    if e.get("email_address"):
        lines.append(f"Email: {e['email_address']} ({e['email_confidence']}% confidence)")
    if e.get("research_attempted"):
        count = e.get("research_verified_count", 0)
        if count > 0:
            lines.append(f"Research verified ({count} fact(s) checked for recency + sentiment)")
        else:
            lines.append("Research attempted, nothing verified — check claims manually before sending")
    lines.append(f"Product: {e['product_summary']}")
    lines.append("")
    lines.append(f"Message:\n{e['message']}")

    return "\n".join(lines) + "\n"


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
    body = "\n---\n".join(format_entry(e) for e in entries)

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
