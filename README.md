# Family Bonus Tracker — Discord Bot

A Discord bot that automates event bonus calculations from user screenshot
submissions, routes them through an interactive admin approval workflow,
calculates bonuses directly inside Discord, and exports the totals to a live
**Google Sheet** whenever an admin requests it.

## Features

- **Supported events:** Informal, Biz War, State, Weapon Factory, Rating Battle, Foundry
- **Submission parsing:** extracts event type, mentions, and kill counts (`3k`, `0 kills`, etc.)
- **Screenshot validation:** confirms the attachment is a real image (Pillow)
- **Admin approval UI:** persistent `✅ Approve` / `❌ Reject` buttons that survive bot restarts
- **Automatic reactions:** `⏳` → `✅` / `❌` on the original submission
- **In-Discord bonus calculation:** each participant's bonus is computed and shown in the approval embed
- **Google Sheet export:** admin runs `!export` to push all approved data to a live Google Sheet (with a shareable link)
- **Quick stats:** `!stats` shows totals + top earners directly in Discord
- **Rejection notifications:** DMs the original submitter when rejected
- **Configurable rates:** all rates and event definitions in `config.py`

## Project Structure

```
.
├── main.py             # Bot entry point, submission listener, !export / !stats commands
├── config.py           # Centralized rates, event types, env-loaded config, calculate_bonus()
├── parsers.py          # Regex parsing (event type, mentions, kills, datetime)
├── sheets.py           # Google Sheets handler (service account auth + export)
├── views.py            # Persistent ApprovalView (approve/reject buttons)
├── requirements.txt    # Python dependencies
├── .env.example        # Template for environment variables
└── README.md           # This file
```

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a new **Application** → **Bot** → reset the token (save it).
3. Enable the **Privileged Gateway Intents**:
   - Message Content Intent
   - Server Members Intent
4. Invite the bot to your server with the **bot** + **applications.commands**
   scopes and the following permissions:
   - Send Messages, Read Message History
   - Add Reactions, Manage Messages (to remove the ⏳ reaction)
   - Embed Links, Attach Files

### 3. Google Sheets service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project, enable **Google Sheets API** and **Google Drive API**.
3. Create a **Service Account**, then create a JSON key and download it.
4. Save the JSON file as `service_account.json` in the bot directory (local dev).
5. On Railway, paste the **full JSON contents** into the `GOOGLE_SERVICE_ACCOUNT_JSON` env var (or base64-encode it).

> The bot creates the `Family_Bonus_Tracker` spreadsheet automatically and sets it to "anyone with the link can view". No manual sharing required.

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in the values:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
SUBMISSION_CHANNEL_ID=123456789012345678
ADMIN_APPROVAL_CHANNEL_ID=123456789012345678
ADMIN_ROLE_IDS=123456789012345678,987654321098765432
GOOGLE_SERVICE_ACCOUNT_JSON=./service_account.json
GOOGLE_SHEET_NAME=Family_Bonus_Tracker
```

To find channel/role IDs: enable **Developer Mode** in Discord
(Settings → Advanced), then right-click → **Copy ID**.

### 5. Run the bot

```bash
python main.py
```

## How users submit

In the designated submission channel, a user posts:

```
Informal 11/10 18:00
@User1 3k @User2 0k @User3 12k
[attached screenshot]
```

The bot:
1. Validates the image and format.
2. Adds a `⏳` reaction to the submission.
3. Posts an embed in `#admin-approval` with Approve/Reject buttons.

## Admin workflow

- **Approve:** buttons disable, embed footer shows "✅ Approved by @Admin",
  the original message's `⏳` becomes `✅`, and each participant's calculated
  bonus is shown in the embed and stored locally.
- **Reject:** buttons disable, footer shows "❌ Rejected by @Admin", the
  original `⏳` becomes `❌`, and the submitter receives a DM.

## Bonus calculation

```
Total Bonus = Base Attendance ($10,000) + (Kills × Rate Per Kill)
```

Default kill rates (editable in `config.py`):

| Event            | Rate / Kill |
|------------------|-------------|
| Informal         | $5,000      |
| Biz War          | $10,000     |
| State            | $7,000      |
| Weapon Factory   | $8,000      |
| Rating Battle    | $10,000     |
| Foundry          | $6,000      |

## Commands

### `!export` — Push to Google Sheet
Any admin runs `!export`. The bot:
1. Opens (or creates) the `Family_Bonus_Tracker` Google Sheet.
2. Clears & rewrites two tabs:
   - **Summary** — per-user totals (Username, User ID, Total Kills, Total Bonus, Events Attended) sorted by bonus, with a grand-total row.
   - **Details** — one row per approved participant (Date, Event, Submitter, User ID, Username, Kills, Bonus).
3. Sets the sheet to "anyone with link can view".
4. Posts the shareable link in Discord.

### `!stats` — Quick inline stats
Shows total bonus, total kills, approved submissions, and a top-10 earners leaderboard directly in a Discord embed.

## Persistence

All submissions (pending + approved + rejected) are saved to `submissions.json`.
On restart, the bot re-registers the approval views so buttons on existing
pending admin embeds keep working.

> **Railway note:** Railway's filesystem is **ephemeral**, so `submissions.json`
> is wiped on every deploy/restart. This means:
> - Pending buttons on old admin embeds stop working after a redeploy.
> - Approved submission history (and thus the `!export` data) is also lost.
>
> To keep history across restarts, attach a **volume** to the service in
> Railway and set `SUBMISSIONS_STORE` (in `config.py`) to a path inside that
> volume (e.g. `/data/submissions.json`).

## Deploying to Railway

1. Push this project to a GitHub repo and connect it to Railway.
2. Railway auto-detects Python and installs `requirements.txt`.
3. **Set the start command.** The repo ships with both:
   - `Procfile` (`worker: python main.py`)
   - `railway.json` (`startCommand: "python main.py"`)
   Using a **worker** process (not `web`) avoids Railway's port health-check
   killing the bot. In the Railway service settings, make sure the process
   type is **worker**, not **web**.
4. **Add environment variables** in Railway → Variables (do NOT rely on a
   `.env` file — Railway doesn't use it):
   - `DISCORD_BOT_TOKEN`
   - `SUBMISSION_CHANNEL_ID`
   - `ADMIN_APPROVAL_CHANNEL_ID`
   - `ADMIN_ROLE_IDS`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` → paste the **full JSON contents** of your
     service account key (one line), or a base64-encoded version.
   - `GOOGLE_SHEET_NAME` (optional, defaults to `Family_Bonus_Tracker`)
5. **Enable Discord intents** in the Developer Portal → Bot:
   - Message Content Intent
   - Server Members Intent
   Without these the bot crashes with `PrivilegedIntentsRequired`.

### Common Railway crashes & fixes

| Symptom | Fix |
|---------|-----|
| `DISCORD_BOT_TOKEN is not set` | Add it in Railway → Variables |
| `PrivilegedIntentsRequired` | Enable Message Content + Members intents in Discord portal |
| Killed with "port/health check" errors | Use `worker` process type, not `web` (see Procfile) |
| `SUBMISSION_CHANNEL_ID` is `0` | Copy the channel ID from Discord (Developer Mode → right-click → Copy ID) |
| `!export` fails | Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the raw JSON (not a file path) and ensure Sheets + Drive APIs are enabled in Google Cloud Console |
