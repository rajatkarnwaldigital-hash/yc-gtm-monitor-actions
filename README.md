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
5. **Researches the best-fit founder** (optional, needs an Exa key) — pulls their recent public
   activity, a competitor or two, and a real account signal describing the exact pain point the
   role is hiring for, then fact-checks every claim for recency and relevance before it's allowed
   anywhere near the message (see [Researching before you reach out](#researching-before-you-reach-out)
   below).
6. **Drafts** a concrete 90-day pilot plan for each founder via Claude, not a generic pitch — a
   specific Day 1 action and Day 90 outcome tied to that company's actual buyer and motion, grounded
   in the job description and any verified research (see
   [Researching before you reach out](#researching-before-you-reach-out) below).
7. **Emails you a digest** every morning with the role, the job link, the founder's LinkedIn, a
   line showing exactly which fact or JD detail the pilot plan is grounded in, and the ready-to-send
   message — you just need to decide who to actually message.

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
[yc_gtm_monitor.py](yc_gtm_monitor.py#L50):

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
- **The outreach message itself** — there are three prompt templates near the top of
  `yc_gtm_monitor.py`, one per tier: `MESSAGE_PROMPT` (bare fallback, used only when neither a job
  description nor research is available), `MESSAGE_PROMPT_JD_ONLY` (a pilot plan grounded in the
  job description alone), and `MESSAGE_PROMPT_RESEARCHED` (a pilot plan grounded in verified
  founder research, falling back to the JD where research doesn't cover it). Edit all three
  together if you're changing the tone or structure — see
  [From research to a pilot plan, not a pitch](#from-research-to-a-pilot-plan-not-a-pitch) above
  for what the latter two currently produce.
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

## Researching before you reach out

If `EXA_API_KEY` is set, the script does one more thing before drafting a message: it fetches the
actual job description off the specific `/jobs/{id}` page (not just the title, which is all the
index-page scrape sees), then runs a bounded set of Exa searches — 2 for the best-fit founder's
recent public activity, 2 for competitors, 2 for a real account signal (a blog post, GitHub issue,
or changelog describing the exact pain point the role is hiring to solve) — for the one founder
already flagged as the best fit. Never every founder at a multi-founder company, and never for a
company with no clear best fit; that's what keeps this predictable at YC-wide scale instead of
scaling with founder count.

Every fact that comes back goes through a separate, cheap Claude call before it's allowed into the
message prompt, checking two things specifically:

- **Recency** — does the source's actual byline or publish date fall within roughly the last 12
  months, not just a generic "updated" timestamp stamped over older content?
- **Sentiment match** — does the source actually describe the pain point or thesis being claimed,
  rather than a success story or a "we solved this" resolution being misread as a problem?

Unverified facts are discarded, not softened or passed through with a caveat. If nothing comes back
verified — or Exa or the verification call errors out or times out — the message falls back to
being grounded in the job description alone instead of founder research; the entry is never dropped
just because research failed.

### From research to a pilot plan, not a pitch

The message itself isn't a "saw your JD, here's what I build" template — that pattern is easy to
spot as AI-written, and it's zero signal since anyone can read the same posting and say the same
thing. Instead, each message proposes a specific 90-day pilot for that company:

1. One line grounding the plan in the real fact or JD detail
2. **Day 1:** a concrete first action naming the actual buyer, segment, or channel this company
   sells to or hires against
3. **Day 90:** a specific, believable outcome scaled to that company's stage — a real number or
   artifact, not vague "pipeline"
4. A soft yes or no ask

Verified research is preferred as the grounding for beats 2 and 3 when it's available (the
`researched` tier); the job description is the fallback when it isn't (`jd_only`). Either way, the
digest shows a **"Pilot angle grounded in:"** line right above the message, stating in plain
language exactly which fact or JD detail the plan is built on — not just a fact count — so you can
verify it before sending, the same way the recency/sentiment checks above let you trust "Research
verified" instead of taking it on faith.

## Required secrets

Set these as GitHub Actions repository secrets (Settings → Secrets and variables → Actions) — see
[DEPLOY.md](DEPLOY.md) for the full walkthrough.

- `ANTHROPIC_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD` — a Gmail [App Password](https://myaccount.google.com/apppasswords), not your normal password
- `RECIPIENT_EMAIL`
- `HUNTER_API_KEY` _(optional)_ — from [hunter.io](https://hunter.io). When set, each digest entry includes the founder's work email if Hunter finds one at ≥70% confidence. Omit entirely to skip email enrichment.
- `EXA_API_KEY` _(optional)_ — from [exa.ai](https://exa.ai). When set, the best-fit founder for each new role gets researched and fact-checked before their message is drafted. See [Researching before you reach out](#researching-before-you-reach-out) above. Omit entirely to skip research and keep the lighter-weight message generation.

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

This came from a real bug, but it's worth being precise about where: it showed up on the
**Railway deployment** of this same script (a separate, private project, not this repo), not
here. Railway has no outbound IPv6 route, and Gmail's SMTP hostname resolves to both an IPv6 and
IPv4 address — `smtplib` trying the IPv6 one first failed with `OSError: [Errno 101] Network is
unreachable`. After forcing IPv4-only resolution, a deeper issue surfaced there: Railway blocks
outbound SMTP entirely (both port 587 and 465 timed out, while plain HTTPS scraping worked the
whole time), so that deployment now sends through Resend's HTTP API instead.

GitHub Actions' hosted runners don't have that restriction — a public example confirms plain
Gmail SMTP sends fine from a standard Actions workflow — so **this repo keeps Gmail SMTP**. That
also keeps setup lower-friction for anyone forking this: a Gmail App Password needs nothing more
than a Gmail account, while Resend would require owning a domain to verify, which most people
asking for this don't have lying around. Either way, the seen-state safeguard above means a
failed send never costs an actual lead — it just retries on the next run.

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

## Build log: what broke and what we fixed

This didn't work perfectly on the first deploy. Most "I built X" posts skip this part, so here's
the honest version, in order:

1. **First working version** — YC company scrape, GTM keyword filter, founder enrichment,
   Claude-drafted messages, digest email.
2. **Bug: failed sends silently lost leads** — the script marked a day's new roles as "seen"
   right after attempting to email them, even if the send failed. A single bad send meant that
   role would never be retried or reported again. Fixed: a role only gets marked seen once the
   email is confirmed delivered; failed sends retry on the next run instead of vanishing. See
   [Reliability](#reliability-failed-sends-dont-lose-leads) above.
3. **Bug (Railway-specific): SMTP failed with `Errno 101: Network is unreachable`** — Railway has
   no outbound IPv6 route, and Gmail's SMTP hostname resolves to both an IPv6 and IPv4 address.
   GitHub Actions' hosted runners don't appear to have this restriction. Fixed (temporarily) by
   forcing IPv4-only DNS resolution.
4. **Bug: job identity collisions** — roles were matched by `company::title`, which silently
   collides if two roles share a title, or breaks if a company edits a title later. Fixed: roles
   are matched by their unique job URL instead, with an automatic one-time migration so existing
   tracked roles don't get reflagged as new.
5. **Feature: best-fit founder flagging** — when a role has multiple founders, one extra Claude
   call uses each founder's YC bio text to recommend who to contact first (e.g. a CEO with
   explicit sales background over a technical co-founder), with a one-sentence reason. See
   [Picking who to reach out to first](#picking-who-to-reach-out-to-first) above.
6. **Bug: misleading subject line** — "3 new roles" for one role shared across three founders.
   Fixed: the subject now counts distinct roles and shows founder count separately, e.g.
   "1 new role (3 founders)".
8. **Feature: Hunter email enrichment** — when `HUNTER_API_KEY` is set as a secret, the script
   looks up each founder's work email via Hunter's email-finder API and includes it in the digest
   entry if confidence is ≥70%. Below that threshold the field is omitted rather than shown as a
   low-confidence guess. Fully optional — the script works identically without this secret.

7. **Bug (Railway-specific, not this repo): SMTP was blocked outright** — even after the IPv6 fix,
   both port 587 and port 465 timed out on the Railway deployment, while plain HTTPS scraping
   worked the entire time. That's the signature of a host silently dropping outbound SMTP traffic
   rather than refusing it. A public example of Gmail SMTP working fine from a standard GitHub
   Actions workflow confirmed Actions runners don't have this restriction, so **this repo stays on
   plain Gmail SMTP** — lower setup friction for anyone forking it, since it needs nothing beyond a
   Gmail account. See [Reliability](#reliability-failed-sends-dont-lose-leads) above for the full
   story.
9. **Feature: real job descriptions + verified research** — messages used to be drafted from just
   a founder name and YC's one-line company blurb, which made them generic. Now the script fetches
   the actual job posting text and, when `EXA_API_KEY` is set, researches and fact-checks the
   best-fit founder before drafting. The fact-checking step exists because we caught two failure
   modes doing this by hand first: a 2022 post re-stamped with a 2026 "updated" date, and a
   "we scaled fine" success story misread as a pain point. Both get caught automatically now
   instead of trusting a single pass to "be careful." See
   [Researching before you reach out](#researching-before-you-reach-out) above.
10. **Bug: research verification silently failed 100% of the time** — every single verification
    call came back as an empty, unparseable response ("Expecting value: line 1 column 1 (char 0)"),
    across every fact, for days, so every digest entry read "nothing verified" regardless of what
    Exa actually found. Root cause: the verification model defaults to adaptive extended thinking
    when the `thinking` parameter is omitted, and the whole `max_tokens` budget was being consumed
    by invisible reasoning before any visible answer token got emitted. Fixed by explicitly passing
    `thinking={"type": "disabled"}` on every Claude call in the file — none of them need multi-step
    reasoning (classify a fact, pick a name, draft a short message), and every call now also checks
    for an empty response with a clear diagnostic instead of a cryptic JSON error if it recurs.
11. **Bug: messages randomly switched between first and third person** — the prompt said a message
    was written "on behalf of Rajat," which left it ambiguous whether to write as Rajat or about
    him; two founders at the same company could get one of each in the same run. Fixed: every
    message prompt now explicitly instructs first person only, and `generate_message()` checks its
    own output for third-person language and retries once with a corrective prompt if it slips
    through, flagging it plainly in the digest if it's still there after the retry.
12. **Bug: every message looked founder-personalized whether it actually was** — the "verified
    research" feature above existed, but every hook still just referenced the job description, and
    nothing in the digest distinguished a message actually grounded in founder research from one
    that wasn't. Fixed: `generate_message()` now only claims founder-specific knowledge when there's
    at least one verified fact for that founder; otherwise it's explicitly told not to imply
    personal knowledge it doesn't have, and every entry states plainly which basis its message used.
13. **Bug: "best fit" reasoning was list order dressed up as a finding** — recommendations like
    "listed first, likely CEO" or "alphabetically first with no differentiating info" were a default
    presented as a signal, not an actual one, because the prompt told the model to always pick
    someone even when nothing distinguished the founders. Fixed: the model is now told that's not a
    signal, and to say "no distinguishing signal" honestly instead of manufacturing a reason — those
    roles now fall back to every founder getting the same non-personalized message, with nobody
    incorrectly flagged `[BEST FIT]`.
14. **Feature: cross-lead triage** — a 10-company, 20-founder digest presented every lead as equally
    worth acting on right now, with no way to tell what actually deserved same-day attention. Added
    one extra ranking call (only once there are enough leads to bother triaging) that flags the 2-3
    most worth acting on today with a one-line reason each, without removing or downranking anyone
    else in the digest.
15. **Bug: a malformed LinkedIn URL reached the digest** — a YC page emitted a founder's LinkedIn
    href with a doubled scheme (`https:https://...`). Added URL validation both at the scrape source
    and as a final pass right before the digest sends, dropping anything that doesn't parse as a
    real absolute URL rather than forwarding it broken.
16. **Bug: verification was STILL failing after the "thinking" fix** — #10 above was a real fix, but
    not the whole story: the very next production run showed the identical "Expecting value: line 1
    column 1 (char 0)" error on every verification and lead-ranking call. Root cause, found with a
    standalone diagnostic script that skipped the full company scrape and hit the API directly with
    shaped test calls: Claude wraps its JSON replies in a Markdown code fence
    (`` ```json\n{...}\n``` ``) even when told to respond with ONLY JSON — `json.loads()` on that
    fails at the leading backtick. Fixed by stripping code fences before parsing everywhere the
    script parses JSON out of a Claude response.
17. **Feature: concrete Day 1/Day 90 pilot plans instead of cite-then-pitch messages** — messages
    used to open by quoting a JD line or research fact, then pivot to a generic "I build signal
    systems, automated sequences, and GTM agents" capability statement, identical across every
    message regardless of company — exactly the pattern that reads as AI-written, since anyone can
    quote the same JD line back. Replaced with a four-beat structure (a grounding line, a specific
    `Day 1:` action naming the actual buyer/channel, a specific `Day 90:` outcome scaled to the
    company's motion, and an ask), plus a "Pilot angle grounded in:" line in the digest showing
    exactly which fact or JD detail the plan is built on. See
    [From research to a pilot plan, not a pitch](#from-research-to-a-pilot-plan-not-a-pitch) above.

---

Built by [Rajat](https://www.linkedin.com/in/rajat-karnwal/), a GTM Engineer who builds outbound
and signal infrastructure for early-stage startups. If you're a founder hiring for GTM and want
something like this running for your own company in a week, that's literally what the generated
messages say — so feel free to take that seriously.
