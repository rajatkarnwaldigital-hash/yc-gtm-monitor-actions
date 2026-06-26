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
2. **Diffs** against what it saw yesterday, so you only ever hear about genuinely new postings.
3. **Enriches** each new posting with the founder's name, title, and LinkedIn URL, pulled straight
   off the company's YC page (one outreach entry per founder, if there are several).
4. **Drafts** a short, non-templated LinkedIn message for each founder via Claude, referencing the
   specific role and a one-week build timeline.
5. **Emails you a digest** every morning with the role, the founder's LinkedIn, and the
   ready-to-send message — you just need to decide who to actually message.

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
