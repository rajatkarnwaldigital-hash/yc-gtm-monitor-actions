# YC GTM Job Monitor

A daily, fully automated system that watches every YC-backed company for new GTM, sales, and
growth hires the day they go live, figures out who the founders are, and drafts a personalized
outreach message to each one — all before you've had coffee.

## The problem this solves

When a YC startup posts a "Founding AE" or "Head of Growth" role, that's a signal: they have
budget, urgency, and usually no GTM infrastructure yet. The people who reach out in the first 24
hours get a very different reception than the people who find the posting two weeks later from a
LinkedIn algorithm. The hard part isn't knowing this — it's the manual grind of checking thousands
of company pages every single day, then figuring out who the founder is, then writing something
that isn't an obvious template.

This system does that grind for you:

1. **Watches** all ~6,000 YC companies' pages daily for roles matching a GTM/sales/growth keyword
   list (see [Customizing the keyword list](#customizing-the-keyword-list) below).
2. **Diffs** against what it saw yesterday by each job's URL, not its title, so you only ever hear
   about genuinely new postings, even if a company reuses a title or edits one later.
3. **Enriches** each new posting with every founder's name, title, LinkedIn URL, and YC bio text,
   pulled straight off the company's YC page (one outreach entry per founder, if there are
   several).
4. **Picks who to reach out to first** when there are multiple founders, based on their bio (see
   [Picking who to reach out to first](#picking-who-to-reach-out-to-first) below) — you still get
   every founder, but one is flagged as the best fit with a reason.
5. **Drafts** a short, non-templated LinkedIn message for each founder via Claude, referencing the
   specific role and a one-week build timeline.
6. **Emails you a digest** every morning with the role, the job link, the founder's LinkedIn, and
   the ready-to-send message — you just need to decide who to actually message.

## How it runs

This repo is set up to run on **GitHub Actions** — no server, no hosting bill. A scheduled
workflow ([.github/workflows/yc_gtm_monitor.yml](.github/workflows/yc_gtm_monitor.yml)) runs the
script once a day and commits the updated state file back into the repo, which is what lets it
remember what it already told you about without paying for a database or a persistent disk.

```
YC API (paginated) ──▶ scrape each company's YC page ──▶ filter to GTM-relevant roles
       │                                                          │
       ▼                                                          ▼
 founder cards (name, title, LinkedIn)              diff against seen_jobs.json
       │                                                          │
       └──────────────────────┬───────────────────────────────────┘
                               ▼
                  new (role, founder) pairs only
                               │
                               ▼
                 Claude drafts one outreach message per pair
                               │
                               ▼
                    Gmail SMTP sends you one digest email
```

## Security note

No API keys, passwords, or email addresses are anywhere in this code. The four credentials it
needs are stored as encrypted [GitHub Actions Secrets](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions)
on the repo, injected as environment variables only at the moment the workflow runs, and never
written to disk or logged. See [DEPLOY.md](DEPLOY.md) for exactly how to set them up if you're
forking or cloning this for your own use.

## Customizing the keyword list

The roles this watches for are controlled by one list near the top of
[yc_gtm_monitor.py](yc_gtm_monitor.py#L52):

```python
GTM_KEYWORDS = [
    "growth", "gtm", "go-to-market", "sales", "marketing", "founding ae",
    "sdr", "bdr", "revenue", "demand gen", "outbound", "automation",
    "lead gen", "business development", "founding account executive",
    "founding sales",
]
```

A job title is flagged if it contains **any** of these words or phrases (case-insensitive,
substring match — so `"sales"` also matches `"Sales Engineer"` or `"Enterprise Sales Lead"`).

To tailor it to what you actually want to see:
- **Add a keyword**: append a new string to the list, e.g. `"customer success"` or `"partnerships"`.
- **Remove a keyword**: delete the line for it — e.g. if `"automation"` is too noisy and matching
  unrelated engineering roles, just remove that entry.
- **Narrow a broad match**: substring matching means `"sales"` catches a lot. If you only want
  founding/early sales hires, replace it with something more specific like `"founding sales"` and
  `"head of sales"` instead of the bare `"sales"`.

No other code needs to change — the filter (`GTM_PATTERN`, built automatically from this list)
is what every scraped job title gets checked against.

You can also adjust:
- **The outreach message itself** — edit the `MESSAGE_PROMPT` template (also near the top of
  `yc_gtm_monitor.py`) to change the tone, length, or call to action.
- **The schedule** — edit the `cron:` line in `.github/workflows/yc_gtm_monitor.yml` (uses standard
  cron syntax, currently `30 3 * * *` = 3:30am UTC daily).

## Picking who to reach out to first

When a role has two or more founders, the script makes one extra Claude call before drafting any
messages: it hands over every founder's name, title, and YC bio text, and asks for the single best
fit to reach out to about that specific role, plus a one-sentence reason.

A real example from a live run, Mastra hiring for Founding Sales with three co-founders:

- **Sam Bhagwat** — Founder/CEO. Bio: "...scaled [Gatsby.js] to $5M ARR, sold to Netlify...
  spent two years knocking doors."
- **Abhi Aiyer** — Founder/CTO. Bio: "Principal eng & lead of >100 person eng org... built infra
  that ran 10s of thousands of build nodes."
- **Shane Thomas** — Founder/CPO. Bio: "Staff eng / head of product... 15+ years in open source."

The system flagged Sam: a CEO with literal door-knocking sales experience called out in his own
bio is a clearly better fit for a sales hire than either of the two technical co-founders. The
digest still includes generated messages for all three (so you have a fallback if Sam doesn't
respond), with Sam's entry labeled `[BEST FIT]` and the reasoning attached.

This intentionally does not scrape LinkedIn itself (follower counts, post activity, etc.) to make
this call — that's against LinkedIn's terms of service and fragile besides. The YC bio text is
already a strong, freely available signal, so that's the ceiling of what this pulls.

## Required secrets

Set these as GitHub Actions repository secrets (Settings → Secrets and variables → Actions) — see
[DEPLOY.md](DEPLOY.md) for the full walkthrough.

- `ANTHROPIC_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD` — a Gmail [App Password](https://myaccount.google.com/apppasswords), not your normal password
- `RECIPIENT_EMAIL`

## First run

The first run populates `seen_jobs.json` with every currently-open GTM role as a baseline and
exits without sending an email — there's nothing "new" to report yet. From the second run onward,
only roles that weren't in that baseline trigger founder enrichment, message generation, and the
email digest.

## Reliability: failed sends don't lose leads

A role only gets marked as "seen" after the digest email actually sends successfully. If the
email send fails for any reason (a transient network issue, bad credentials, Gmail being Gmail),
that role is left out of `seen_jobs.json` on purpose, so the next run sees it as new again and
retries the whole thing, founder enrichment and message generation included, instead of silently
dropping it.

This came from a real bug: on containerized hosts (Railway, and potentially GitHub Actions
runners too) outbound IPv6 routing is often missing, and Gmail's SMTP hostname resolves to both
an IPv6 and IPv4 address. `smtplib` trying the IPv6 one first failed with `OSError: [Errno 101]
Network is unreachable`, which the script caught and logged, but it still went ahead and marked
that day's new roles as seen, permanently losing them even though the email never arrived. The
script now forces IPv4-only DNS resolution to avoid the failure in the first place, and as a second
line of defense, only advances the seen-state when the send is confirmed successful.

`seen_jobs.json` keys roles by their job URL rather than `company::title`, since two roles can
share a title (or a company can edit one later), which would otherwise cause a missed or
duplicate alert. If you're updating from an older version of this repo that used the
`company::title` key, the script migrates existing entries onto their URL automatically on the
next run, so you won't get a flood of false "new role" alerts for things you'd already seen.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
export $(grep -v '^#' .env | xargs)
python3 yc_gtm_monitor.py
```

---

Built by [Rajat](https://www.linkedin.com/in/rajat-karnwal/), a GTM Engineer who builds outbound
and signal infrastructure for early-stage startups. If you're a founder hiring for GTM and want
something like this running for your own company in a week, that's literally what the generated
messages say — so feel free to take that seriously.
