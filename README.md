# Gmail Autoclean

Cleans out your Gmail backlog one sender at a time and sends you a digest three times a day. It runs on GitHub Actions, so your computer doesn't need to be on.

First-time setup is in **[SETUP.md](SETUP.md)**.

## What it does

### Backlog cleanup (you make every call)
- **Backlog scan** reads only the headers of every email and groups them by sender into the **Review Senders** tab of your Google Sheet. For each sender you see:
  - how many emails they've sent, and how many are unread
  - the dates of their first and last email
  - three sample subject lines
  - whether you've ever emailed them
  - whether they support unsubscribe
  - a rough "looks spammy" hint
- You pick a **Decision** from the dropdown on each row:

  | Decision | What happens |
  |---|---|
  | **Delete** | Trashes all their mail, unsubscribes you, and adds a filter that deletes anything new from them |
  | **Delete (no unsubscribe)** | Same, but skips unsubscribing. Use this for real spam, since unsubscribing just confirms your address is live |
  | **Delete whole domain** | Same, for every sender at that domain (e.g. all of `quora.com`). Senders you marked Keep, or listed in Always Important, are left out. It refuses to run on gmail.com, yahoo.com, and other personal email domains, and on companies you log into (Microsoft, Google, Apple, Amazon, banks, job sites, .gov/.edu, and so on). On any other domain, addresses that have sent sign-in codes, receipts, or application emails are kept automatically and left out of the filter |
  | **Keep** | Nothing happens, and the row moves down out of your way |
  | **Later** / blank | Nothing happens yet |

- Once a sender is deleted, its row moves to the **Deleted Senders** tab so Review Senders only shows what's left to decide. Review Senders stays sorted: undecided senders (biggest first), then rows that were skipped and need a different choice, then everything you marked Keep at the bottom.
- Decisions are applied at the next digest run, or right away if you use **Actions > Apply decisions > Run workflow**. That works from the GitHub phone app too. Afterward, the **Status** column shows what happened.
- Deleted mail goes to **Trash**, which Gmail empties after 30 days. If you change your mind within that window, you can pull it back out.
- The filters send new mail straight to Trash, so nothing piles up. Senders are packed several to a filter to stay under Gmail's limit of about 1,000 filters. You can see or remove the filters in Gmail under **Settings > Filters and Blocked Addresses**.
- If a sender only offers a web page to unsubscribe, the digest lists it under **Finish unsubscribing** so you can tap it.

### Digests (9 AM, 2 PM, 8 PM Central)
Each new email gets a quick rating from Claude (Haiku) with a one-line reason:

| Tier | Gmail label | Meaning |
|---|---|---|
| Needs you | `!Priority` + star | Real people, recruiters, interviews, bills due, security alerts, anything time-sensitive |
| Worth a look | `Digest/Worth a look` | Useful but not urgent. Anything even slightly important, or that the rating isn't sure about, lands here or higher |
| Low | `Digest/Low` | Legit automated mail: receipts, shipping, notices |
| Probably junk | `Digest/Probably junk` | Promos, social notifications, digests |

**Nothing is ever deleted based on a rating.** Emails only get labeled. The only mail that gets deleted is from senders you marked Delete in the sheet. Senders rated junk that you haven't reviewed yet get added to your review list.

The digest email includes:
- **Needs you** and **Worth a look**, with a summary of each email
- **Job search updates**, which are also logged to the **Job Tracker** tab (company, role, status, last update)
- **Suggested for your calendar**: interviews, appointments, and deadlines, each with an "Add to calendar" link. Nothing is added to your calendar automatically. These are also saved to the **Events** tab.
- **Bills & receipts**, also saved to the **Bills & Receipts** tab
- **Cleanup** stats and any new senders waiting for review
- Short lists of the Low and junk emails

### Auto-clean (optional, off until you turn it on)
The goal: obvious junk disappears on its own, and anything it isn't sure about is left for you.

**It's conservative on purpose.** An email is only auto-deleted when *all* of these agree:
1. No keep rule applies. Keep rules cover: people, anyone you've emailed, threads you replied in, starred mail, receipts/orders/deliveries, job search, tax/legal docs, anything with an attachment, sign-in codes under 2 weeks old, and your Always Important list.
2. The model trained on your own Delete/Keep decisions is 90%+ sure.
3. At least 2 separate junk signals show up (Promotions/Social tab, promo subject, you never open that sender, mailing-list header).
4. **Claude reads the body** and is at least 90% sure it's junk. If anything in the body looks like it could matter, it goes to Review instead.

The other auto-deletes, which don't need the checks above:
- **Quiet senders.** If a sender, or anyone at that company's domain, hasn't emailed you in `dormant_years` (default 3), their mail goes. These are still kept: receipts/orders, tax/legal docs, starred mail, threads you replied in, people you've emailed, and personal Gmail/Yahoo-type senders. Anything with an attachment goes to Review.
- Sign-in codes older than 2 weeks.
- Mail from senders you already deleted that slipped past the filter.

Nothing from a domain on the `never_auto_delete` list is ever auto-deleted. It starts empty. Add domains you never want touched (your school, your employer), and subdomains count too: `myschool.edu` covers `alumni.myschool.edu`.

**Every digest lists what was auto-deleted, with a Recover link** next to each email. Tap it, then **Move to Inbox** in Gmail. On its next run the scanner notices you recovered it, and never auto-deletes that sender again (logged in the **Learned** tab). Everything auto-deleted is tagged `Auto-cleaned` and sits in Trash for 30 days.

**How to turn it on:**
1. Run **Actions > Sweep** with "clean" unchecked, and look over the **Sweep Preview** and **Sweep Summary** tabs.
2. If the Delete rows look right, run **Sweep** again with "clean" checked. That cleans up the backlog.
3. Set `auto_clean: true` in `config.yaml` and push. New mail then gets auto-cleaned at every digest.

Cost: Claude only reads emails that already look like junk, up to 5 per sender. It's capped by `sweep_max_body_checks` and comes to about $1 per 1,000 emails read.

### Teaching it
- **Always Important tab:** add an email address (like `mom@gmail.com`) or a domain (like `acme.com`). Mail from these is always marked Needs you and can never be deleted by the scanner.
- **Correcting it:** if you add the `!Priority` label to something it rated lower, it learns that sender matters. If you remove `!Priority` from something, it learns the opposite. You'll see these in the **Learned** tab, and they feed into future ratings.

## Settings (`config.yaml`)
- `digest_hours`: when digests go out. If you change these, also change the cron line in `.github/workflows/digest.yml` (the comment there explains how).
- `about_me`: what you care about. Claude reads this when rating.
- `archive_low` / `archive_junk`: set to `true` to also move those emails out of your inbox. This archives them; it doesn't delete them.
- `send_empty_digest`: off by default, so you don't get a digest when nothing's worth reading.

## Costs and privacy
- **GitHub Actions:** free. Private repos get 2,000 minutes a month, and this uses around 100 to 200.
- **Claude API:** a couple dollars a month or less at normal volume. The backlog scan doesn't use Claude at all.
- **What leaves Gmail:** the sender, subject, and first ~1,500 characters of each *new* email are sent to Anthropic's API for rating. Your Google login token is stored only in your private repo's encrypted secrets. The backlog scan reads headers only, never message bodies.

## Commands
```
python -m emailscanner auth           # one-time Google sign-in (PC)
python -m emailscanner init-sheet     # one-time sheet creation
python -m emailscanner scan-backlog   # fill/refresh the Review Senders tab
python -m emailscanner apply          # act on your decisions now
python -m emailscanner sweep [--clean] # auto-clean preview, or do it
python -m emailscanner digest --force # send a digest now
python -m emailscanner digest --preview  # write digest_preview.html, change nothing
python -m pytest                      # run tests
```
