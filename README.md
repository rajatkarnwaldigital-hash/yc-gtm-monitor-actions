# YC GTM Job Monitor (GitHub Actions version)

Daily job: pulls every YC company, scrapes each company's YC page for new GTM/sales/growth roles,
enriches with founder data, generates a personalized outreach message per founder via Claude, and
emails a digest.

This is functionally identical to the Railway-deployed version — same script, same logic. The only
difference is *how* it runs and persists state: instead of a Railway Volume, the scheduled
GitHub Actions workflow commits the updated `seen_jobs.json` back into this repo after every run.
That's what makes the "what have I already seen" state survive between runs, with no paid hosting
required.

See [DEPLOY.md](DEPLOY.md) for the full setup walkthrough.

## How persistence works here

1. The workflow checks out this repo (including whatever `seen_jobs.json` is currently committed).
2. The script runs and updates `seen_jobs.json` in the checkout.
3. The workflow's last step commits and pushes that file back to the repo — but only if it
   actually changed (no-op when there are no new roles that day).

## Required secrets

Set these as GitHub Actions repository secrets (not repo variables, not a committed `.env`):

- `ANTHROPIC_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD` — a Gmail [App Password](https://myaccount.google.com/apppasswords), not your normal password
- `RECIPIENT_EMAIL`

## First run

The first run populates `seen_jobs.json` with every currently-open GTM role as a baseline and
exits without sending an email. From the second run onward, only newly-posted roles trigger
founder enrichment, message generation, and the email digest.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
export $(grep -v '^#' .env | xargs)
python3 yc_gtm_monitor.py
```
