# Deploying via GitHub Actions

No Railway account, no Volume, no billing — this runs entirely inside GitHub's free Actions minutes.

## 1. Add the secrets
The workflow reads four secrets. Set them on **this repo**, not your account globally:

1. Go to the repo on GitHub → **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** and add each of these (one at a time):
   - `ANTHROPIC_API_KEY`
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD` — see Step 2 below if you don't have one yet
   - `RECIPIENT_EMAIL`

Alternatively, from a terminal with `gh` installed and logged in, run from inside this repo's
folder (this keeps the values out of any chat transcript — type them directly into your own
terminal, don't paste them to an assistant):

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set GMAIL_ADDRESS
gh secret set GMAIL_APP_PASSWORD
gh secret set RECIPIENT_EMAIL
```

Each command will prompt you to paste the value — it won't echo it back to the terminal.

## 2. Generate a Gmail App Password (if you don't already have one)
Gmail blocks script logins with your normal password.

1. Go to **myaccount.google.com** → **Security** → turn on **2-Step Verification** if it isn't already on.
2. Go to **myaccount.google.com/apppasswords**.
3. Name it something like "YC monitor" and click **Create**.
4. Copy the 16-character password (no spaces) — that's your `GMAIL_APP_PASSWORD`.

(If you already generated one for the Railway version, you can reuse the same one here.)

## 3. Confirm Actions are enabled
For a private repo created via `gh repo create`, Actions are enabled by default. To double check:
**Settings** → **Actions** → **General** → make sure "Allow all actions" (or similar) isn't disabled.

## 4. Trigger a test run manually
Don't wait for the 3:30am UTC schedule to find out if it works.

1. Go to the **Actions** tab on the repo.
2. Click the **YC GTM Monitor** workflow on the left.
3. Click **Run workflow** (this is the `workflow_dispatch` trigger baked into the workflow file) → **Run workflow**.
4. Click into the run and watch the logs. It scrapes ~6,000 YC company pages, so expect it to take
   several minutes.
5. A clean first run ends with:
   ```
   First run detected — populating baseline, no email will be sent
   Baseline saved: N roles in seen_jobs.json
   DONE (baseline run)
   ```
   **No email is sent on this first run** — that's expected, it's just establishing the baseline.
6. Check the repo's commit history — you should see a new commit "Update seen_jobs.json" from the
   workflow, with the baseline file in it. That confirms persistence is working.

## 5. From here
The schedule in `.github/workflows/yc_gtm_monitor.yml` runs it daily at 3:30am UTC (9am IST)
automatically. Only roles that weren't in the last committed `seen_jobs.json` trigger founder
enrichment, message generation, and an email.

## Things to know
- **Scheduling isn't to-the-second.** GitHub may delay scheduled workflows by a few minutes during
  high platform load. Not an issue for a "once a day, whenever in the morning" job.
- **Auto-disable after 60 days of repo inactivity.** If this repo gets zero commits or other
  Actions activity for 60 days straight, GitHub disables the scheduled trigger and you'd need to
  re-enable it from the Actions tab. Since the workflow itself commits to the repo roughly daily
  whenever new roles are found, this mostly takes care of itself — but if there's a long quiet
  stretch with zero new roles, no commits happen either. Worth a glance every couple of months.
- **Cost.** A few minutes a day is nowhere near GitHub's free Actions minutes allowance for a
  private repo. This should cost nothing.
