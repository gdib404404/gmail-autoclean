# Setup (about 30 minutes, one time)

You'll need your PC for steps 2 through 4. After that, everything runs on GitHub and your PC can stay off.

## 1. Put the code in a private GitHub repo

1. On GitHub, create a new repository called `email-scanner` and set it to **Private**. Private matters for two reasons: GitHub pauses scheduled jobs on public repos that go 60 days without a commit, and your config describes you.
2. Push this folder to that repo:
   ```
   cd email-scanner
   git init
   git add .
   git commit -m "Email scanner"
   git branch -M main
   git remote add origin https://github.com/<your-username>/email-scanner.git
   git push -u origin main
   ```

## 2. Create the Google Cloud app (this lets the script into your Gmail)

1. Go to https://console.cloud.google.com and create a new project called **Email Scanner**.
2. Go to **APIs & Services > Library** and enable both **Gmail API** and **Google Sheets API**.
3. Go to **Google Auth Platform** (older UI: *OAuth consent screen*):
   - App name: `Email Scanner`. Use your Gmail address for both email fields.
   - Audience: **External**.
   - Under **Audience**, click **Publish app** so it moves to **In production**.
     **Don't skip this.** In "Testing" mode, Google expires your login every 7 days and the scheduled runs stop working.
4. Go to **Clients > Create client** and choose **Desktop app**. Download the JSON file, rename it `client_secret.json`, and put it in the `email-scanner` folder. It's gitignored, so it won't get uploaded.

## 3. Sign in once from your PC

In PowerShell, from the `email-scanner` folder:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m emailscanner auth
```

A browser window will open. Google will warn you that it **hasn't verified this app**. That's expected, because the app is yours and has never been submitted to Google for review. Click **Advanced > Go to Email Scanner** and approve.

The terminal then prints three values. Keep them for step 6:
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.

## 4. Create your Google Sheet

```
python -m emailscanner init-sheet
```

This creates a sheet called **Email Scanner** in your Google Drive and prints its link plus a `SHEET_ID`. Bookmark the link. This sheet is where you review senders, and it holds your job tracker, events, and bills.

## 5. Get an Anthropic API key

1. Go to https://console.anthropic.com. Add a payment method and a few dollars of credit under **Billing**.
2. Under **API Keys**, click **Create Key** and copy it.

This is billed separately from a Claude.ai subscription. At normal email volume it costs a couple dollars a month or less.

## 6. Add the secrets to GitHub

In your repo, go to **Settings > Secrets and variables > Actions > New repository secret** and add all five:

| Name | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from step 3 |
| `GOOGLE_CLIENT_SECRET` | from step 3 |
| `GOOGLE_REFRESH_TOKEN` | from step 3 |
| `SHEET_ID` | from step 4 |
| `ANTHROPIC_API_KEY` | from step 5 |

## 7. Scan the backlog

In your repo, go to **Actions > Backlog scan > Run workflow**. With about 39k emails, expect it to take roughly 20 to 40 minutes. When it's done, the **Review Senders** tab lists every sender, with the biggest ones at the top.

## 8. Test a digest

Go to **Actions > Digest > Run workflow**. The "Send a digest right now" box is checked by default. A digest covering the last 24 hours should arrive within a couple of minutes.

After that, digests run on their own at 9 AM, 2 PM, and 8 PM Central.

---

### Optional: preview a digest on your PC without changing anything

```
$env:SHEET_ID="..."
$env:ANTHROPIC_API_KEY="..."
python -m emailscanner digest --preview
```

This writes `digest_preview.html`. It doesn't label anything, doesn't write to the sheet, and doesn't send an email.

### If something breaks

- **"invalid_grant" in the Actions log:** your Google login expired or was revoked. Usually the app was still in Testing mode. Publish it (step 2.3), then redo step 3 and update `GOOGLE_REFRESH_TOKEN`.
- **To see what a run did:** open **Actions**, click the run, and read the log. It lists every sender processed and every email count.
