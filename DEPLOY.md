# Deploying your own copy

This runs entirely on GitHub Actions — no server to manage, no hosting bill, no Railway/AWS/etc.
account needed. Everything below happens inside GitHub's own website.

If you've never deployed anything before, don't worry — every step here is a checkbox or a form
field, no command line required except one optional shortcut in Step 1.

## Step 0: Fork this repo

You need your own copy of this repo so you can add your own secrets to it (secrets are tied to a
specific repo, not shared across forks).

1. Click the **Fork** button at the top right of this repo's GitHub page.
2. Leave the settings as-is and click **Create fork**.
3. You now have your own copy at `github.com/<your-username>/yc-gtm-monitor-actions`. Do
   everything below on **your fork**, not the original.

## Step 1: Get the four values you'll need

Before touching GitHub settings, have these four things ready to paste in:

1. **An Anthropic (Claude) API key** — sign up at [console.anthropic.com](https://console.anthropic.com),
   go to **API Keys**, create one. This is what generates the outreach messages — Claude API usage
   is billed by usage, a few cents a day for this volume.
2. **Your Gmail address** — whichever inbox you want the daily digest sent from.
3. **A Gmail App Password** (NOT your normal Gmail password — Google blocks scripts from using
   your real password):
   - Go to [myaccount.google.com](https://myaccount.google.com) → **Security**.
   - Turn on **2-Step Verification** if it isn't on already (the App Password option won't appear
     until this is enabled).
   - Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   - Name it something like "YC monitor" and click **Create**.
   - Copy the 16-character password it shows you (remove the spaces) — you can't view it again
     after this screen closes, so paste it somewhere safe for the next step.
4. **The email address you want the digest delivered to** — can be the same Gmail address or a
   different inbox entirely.

## Step 2: Add the four secrets to your fork

1. On your fork, go to **Settings** (top tab) → **Secrets and variables** (left sidebar) →
   **Actions**.
2. Click the green **New repository secret** button.
3. Add each of these one at a time — click **New repository secret** again for each:

   | Name (type exactly this, case matters) | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | the Claude key from Step 1 |
   | `GMAIL_ADDRESS` | your Gmail address |
   | `GMAIL_APP_PASSWORD` | the 16-character App Password (no spaces) |
   | `RECIPIENT_EMAIL` | where you want the digest sent |

4. After all four are added, the **Secrets** list should show 4 entries (the values themselves
   stay hidden — that's expected, GitHub never shows secret values again after you save them).

## Step 3: Make sure Actions are enabled

Forked repos sometimes have Actions disabled by default as a safety measure.

1. Go to the **Actions** tab (top of the repo).
2. If you see a banner saying workflows are disabled, click the button to enable them.

## Step 4: Run it for the first time

1. Still on the **Actions** tab, click **YC GTM Monitor** in the left sidebar (the workflow name).
2. Click the **Run workflow** dropdown button on the right → **Run workflow**.
3. A new run appears in the list after a few seconds — click into it to watch the logs live.
4. This first run scrapes around 6,000 YC company pages, so it takes several minutes. Don't worry
   if it looks like it's "stuck" — check the logs, you should see a counter like
   `[3400/5975] pages scraped ...` ticking up.
5. A successful first run ends with:
   ```
   First run detected — populating baseline, no email will be sent
   Baseline saved: N roles in seen_jobs.json
   DONE (baseline run)
   ```
   **You will not get an email on this first run** — that's correct behavior. It's just learning
   what roles already exist so it can tell you about new ones tomorrow.
6. To double check it worked, look at your fork's commit history (the **Code** tab) — you should
   see a new commit titled "Update seen_jobs.json". That commit is the proof your setup is
   correctly remembering state between runs.

## Step 5: Let it run on its own

From here, it runs automatically every day at 3:30am UTC (9am IST) with zero further action from
you. You'll get an email any morning a new GTM role shows up that wasn't in the last baseline.

## If something goes wrong

- **The run fails immediately with an error about a missing key** — double check the secret
  *names* in Step 2 are spelled exactly as shown (no typos, no extra spaces) — GitHub Actions
  secrets are case- and spelling-sensitive.
- **It runs but you never get an email** — that's expected if no new GTM roles were posted that
  day. Check the run's logs; it will say `New roles found: 0` near the bottom if so.
- **You want email alerts if a run fails outright** (e.g. the YC site changes its page structure) —
  go to your GitHub profile (top right) → **Settings** → **Notifications** → scroll to the
  **Actions** section and make sure failed-workflow emails are turned on.
