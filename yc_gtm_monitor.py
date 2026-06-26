#!/usr/bin/env python3
"""
Daily YC GTM job monitor.

Pulls every company from the YC public API, scrapes each company's YC page
for open roles + founders, diffs new GTM-relevant roles against seen_jobs.json,
generates a personalized outreach message per founder via Claude, and emails
a digest via Gmail SMTP.

Builds on the page-scraping logic (founder cards, job links) proven out in
yc_prospector.py.
"""

import json
import os
import re
import smtplib
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Containerized hosts (Railway included) often have no outbound IPv6 route.
# Gmail's SMTP hostname resolves to both an IPv6 and an IPv4 address, and
# smtplib trying the IPv6 one first fails with "OSError: [Errno 101] Network
# is unreachable" instead of falling back. Forcing IPv4-only DNS resolution
# avoids that without affecting TLS hostname validation (smtplib still
# connects using the hostname, never a raw IP).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

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

CLAUDE_MODEL = "claude-sonnet-4-6"

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


# ── Step 3 + 4: Founder enrichment + message generation ──────────────────────

def generate_message(client, founder_name: str, company_name: str, yc_batch: str,
                      role_title: str, product_summary: str) -> str:
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
            max_tokens=300,
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
    """One entry per (new role, founder). Companies with no founders get one placeholder entry."""
    print(f"\n[4] Enriching {len(new_jobs)} new role(s) with founder data + generating messages …")
    entries = []

    for job in new_jobs:
        slug = job["slug"]
        company_name = job["company_name"]
        yc_batch = job["yc_batch"]
        role_title = job["title"]
        role_url = job["url"]
        product_summary = job["product_summary"]

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
                "product_summary": product_summary,
                "message": "(no founder data found on YC page)",
                "is_best_fit": False,
                "best_fit_reason": "",
            })
            continue

        best_founder_name, best_fit_reason = "", ""
        if len(founders) > 1:
            best_founder_name, best_fit_reason = pick_best_founder(
                client, founders, company_name, role_title
            )
            if best_founder_name:
                print(f"  Best-fit founder for {company_name}: {best_founder_name} — {best_fit_reason}")

        for f in founders:
            founder_name = f.get("name", "Unknown")
            print(f"  Generating message: {founder_name} @ {company_name} — {role_title}")
            message = generate_message(
                client, founder_name, company_name, yc_batch, role_title, product_summary
            )
            is_best_fit = bool(best_founder_name) and founder_name == best_founder_name
            entries.append({
                "company_name": company_name,
                "yc_batch": yc_batch,
                "role_title": role_title,
                "role_url": role_url,
                "founder_name": founder_name,
                "linkedin_url": f.get("linkedin", ""),
                "product_summary": product_summary,
                "is_best_fit": is_best_fit,
                "best_fit_reason": best_fit_reason if is_best_fit else "",
                "message": message,
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
    lines.append(f"Product: {e['product_summary']}")
    lines.append("")
    lines.append(f"Message:\n{e['message']}")

    return "\n".join(lines) + "\n"


def send_email(entries: list[dict]) -> bool:
    """Returns True only if the email was actually delivered — callers use this
    to decide whether it's safe to mark these roles as seen."""
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
        for job in data["jobs"]:
            all_jobs.append({
                "slug": slug,
                "company_name": company_name,
                "yc_batch": yc_batch,
                "title": job["title"],
                "url": job["url"],
                "product_summary": product_summary,
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
