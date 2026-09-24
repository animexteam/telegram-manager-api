# Telegram Manager API

An HTTP API to operate and manage **multiple Telegram accounts/sessions** from one place. Built with FastAPI + Telethon. Deploys to Render free tier with **GitHub Gist-backed persistence** so your sessions survive Render's sleep cycles.

> ⚠️ **Disclaimer:** This tool is for accounts you own or have explicit permission to manage. Don't use it for spam, fraud, or anything that violates Telegram's Terms of Service. You're responsible for what you do with it.

---

## Features

- `/ping` — wake-up / health endpoint (call this when Render is asleep)
- **Gist persistence** — sessions stored in a *private* GitHub gist; auto-pulled on boot, auto-pushed after every write
- **Upload sessions** — drop in an existing Telethon `.session` file (single or `.zip`)
- **Login manually** — phone → OTP → optional 2FA
- **List chats** — all dialogs, channels, or groups only
- **Send messages** — to any user/channel/group by username or ID
- **Visit chats** — mark as read + pull recent messages
- **Click sponsored ads** — fetch sponsored messages in a channel and "click" them
- **Download backups** — pull the session back as a `.zip` any time

---

## 1. Project Layout

```
telegram-manager-api/
├── app.py              # FastAPI app (endpoints)
├── tele_manager.py     # Telethon client manager + gist sync hooks
├── gist_store.py       # GitHub Gist storage backend
├── requirements.txt
├── render.yaml         # Render Blueprint
├── Procfile            # alt start command
├── .env.example
├── .gitignore
└── README.md
```

---

## 2. Session Upload Format

You can upload sessions two ways.

### Option A — single `.session` file

`POST /accounts/acc_abc123def456/upload` with the file as multipart form-data.

You must also send meta afterwards (or include `api_id` / `api_hash` via `/meta`):

```
POST /accounts/acc_abc123def456/meta
Content-Type: application/json

{
  "api_id": 123456,
  "api_hash": "abcd1234abcd1234abcd1234abcd1234",
  "phone": "+919999999999",
  "name": "personal"
}
```

### Option B — `.zip` (recommended)

Zip structure:

```
my_session.zip
├── session.session       # required — the Telethon SQLite session file
└── meta.json             # optional — see schema below
```

`meta.json` example:

```json
{
  "api_id": 123456,
  "api_hash": "abcd1234abcd1234abcd1234abcd1234",
  "phone": "+919999999999",
  "name": "personal"
}
```

### How to generate a Telethon `.session` file locally

```python
from telethon import TelegramClient
c = TelegramClient("my_session", api_id, api_hash)
c.start(phone="+919999999999")  # will prompt for OTP / 2FA
# This creates my_session.session next to your script
```

Then zip `my_session.session` with a `meta.json` (option B) and upload.

> **Tip:** the `account_id` (e.g. `acc_abc123def456`) is any string you choose. It just needs to be unique. Hit `GET /accounts` first to see which IDs already exist.

---

## 3. GitHub Gist Persistence (solves Render sleep data loss)

Render's **free-tier filesystem is ephemeral**. When the service sleeps (after ~15 min of inactivity) and wakes up again, anything written to disk is lost.

This app solves that by mirroring every account's `session.session + meta.json` into a **private GitHub gist** (only visible to the token owner).

### How it works

| Trigger | Action |
|---|---|
| App starts up | Auto-pulls all `acc_*__*` files from gist → `data/sessions/` |
| `POST /accounts/{id}/upload` | Saves locally + pushes `acc_id__session.session` to gist |
| `POST /accounts/{id}/meta` | Updates meta + pushes `acc_id__meta.json` to gist |
| `POST /accounts/{id}/otp` or `/2fa` | After successful login, pushes the new `session.session` to gist |
| `DELETE /accounts/{id}` | Deletes local files + removes `acc_id__*` from gist |
| `POST /sync/pull` | Manually re-pull all from gist → disk |
| `POST /sync/push` | Manually push all disk → gist |

### File naming in gist

Gist filenames cannot contain `/`. So on disk we have:

```
data/sessions/acc_abc123def456/session.session
data/sessions/acc_abc123def456/meta.json
```

In the gist these become:

```
acc_abc123def456__session.session   (base64-encoded — gist files are text-only)
acc_abc123def456__meta.json         (plain JSON)
```

Binary files (`.session` SQLite DBs) are **base64-encoded** when pushed and **base64-decoded** when pulled. Verified byte-for-byte identical after round-trip.

### One-time setup

1. Generate a GitHub token with the `gist` scope:
   https://github.com/settings/tokens/new?scopes=gist
2. Add to your Render service env vars (or local `.env`):
   ```
   GITHUB_GIST_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxx
   GIST_ID=                         # leave blank on first run
   ```
3. Deploy. On first run, hit `POST /sync/init` — it creates a new private gist and returns its `gist_id`.
4. Copy the returned `gist_id` into the `GIST_ID` env var on Render, then restart the service.
5. From now on, every upload/login/delete auto-syncs to the gist, and the service auto-restores on every wake.

### Quick way to pre-create the gist

You can also create the gist manually beforehand:

```bash
curl -X POST https://api.github.com/gists \
  -H "Authorization: Bearer ghp_YOUR_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{
    "description": "Telegram Manager API session store",
    "public": false,
    "files": {".gistkeep": {"content": "init"}}
  }'
# -> response includes "id" — paste that into GIST_ID env var
```

---

## 4. Local Dev

```bash
cd telegram-manager-api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum set MASTER_API_KEY, GITHUB_GIST_TOKEN, GIST_ID
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open:
- http://localhost:8000/docs        — Swagger UI (interactive)
- http://localhost:8000/ping        — health
- http://localhost:8000/openapi.json — full schema

---

## 5. Deploy to Render

### Method 1 — Render Dashboard (easy)

1. Push this folder to a GitHub repo (e.g. `yourname/telegram-manager-api`).
2. In Render dashboard: **New → Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
   - **Health Check Path:** `/ping`
4. Environment variables:
   - `MASTER_API_KEY` = some long random string (REQUIRED — protects your API)
   - `GITHUB_GIST_TOKEN` = your GitHub token with `gist` scope (REQUIRED for persistence)
   - `GIST_ID` = (leave blank on first deploy)
5. Click **Create Web Service**.
6. After first boot, hit `POST /sync/init` → copy the returned `gist_id` → set as `GIST_ID` env var → redeploy.

### Method 2 — render.yaml Blueprint

1. Push this folder to GitHub (the `render.yaml` is already in the repo).
2. In Render dashboard: **New → Blueprint** → pick the repo.
3. Render reads `render.yaml` and creates the service automatically.
4. After creation: go to the service → **Environment** → set `MASTER_API_KEY`, `GITHUB_GIST_TOKEN`, and (after first `/sync/init` call) `GIST_ID`.

---

## 6. ⚠️ Render Free Tier — Sleep Behavior

Render's free plan sleeps after ~15 min of inactivity. **With Gist persistence, this is fine** — sessions survive in gist.

### Keep-alive (recommended)

Even though sessions survive, the in-memory Telegram clients are lost on sleep. To keep the API warm:

- Use [UptimeRobot](https://uptimerobot.com) (free) or [cron-job.org](https://cron-job.org) → HTTP monitor → `https://<your-service>.onrender.com/ping` every 10 minutes.

This keeps the service responsive. The first request after a sleep takes ~20-30 seconds; subsequent requests are instant.

### Why gist instead of a Render Persistent Disk?

| Option | Cost | Trade-off |
|---|---|---|
| **GitHub Gist** (this app) | Free | Auto-sync on every write. Slight latency (~500ms per gist API call). Perfectly private. |
| Render Persistent Disk | $7/mo (Starter plan) | Faster I/O, no API calls. But requires paid plan. |

Gist is the right choice if you want to stay on the free tier.

---

## 7. API Reference

> All endpoints except `/ping` and `/` require header `X-API-Key: <MASTER_API_KEY>` (only if `MASTER_API_KEY` env is set).

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/ping` | Health check + uptime. Use this to wake Render. |
| `GET` | `/` | Service info. |
| `GET` | `/docs` | Interactive Swagger UI. |

### Sync (GitHub Gist)

| Method | Path | Description |
|---|---|---|
| `GET` | `/sync/status` | Show gist config (token present, gist_id set). |
| `POST` | `/sync/init` | Create a new private gist, return its id. Run once, then set `GIST_ID` env. |
| `POST` | `/sync/pull` | Manually pull all sessions from gist → disk. |
| `POST` | `/sync/push` | Manually push all local accounts → gist. |

### Accounts

| Method | Path | Body | Description |
|---|---|---|---|
| `GET` | `/accounts` | — | List all accounts. |
| `POST` | `/accounts/new` | `{api_id, api_hash, phone, name?}` | Start login flow. Returns `account_id` + `phone_code_hash`. |
| `GET` | `/accounts/{id}` | — | Account info. |
| `POST` | `/accounts/{id}/meta` | `{api_id, api_hash, phone?, name?}` | Set/update metadata for an uploaded session. |
| `DELETE` | `/accounts/{id}` | — | Delete account + session file + gist entry. |
| `POST` | `/accounts/{id}/upload` | `multipart file=.session or .zip` | Upload existing Telethon session. Auto-pushes to gist. |
| `GET` | `/accounts/{id}/download` | — | Download `session.session + meta.json` as a `.zip`. |

### Login flow

| Method | Path | Body | Description |
|---|---|---|---|
| `POST` | `/accounts/{id}/otp` | `{code}` | Submit OTP. If 2FA is on, returns `status: 2fa_required`. Auto-pushes session to gist on success. |
| `POST` | `/accounts/{id}/2fa` | `{password}` | Submit 2FA password. Auto-pushes session to gist on success. |

### Operations

| Method | Path | Body | Description |
|---|---|---|---|
| `GET` | `/accounts/{id}/me` | — | Get the logged-in user. |
| `GET` | `/accounts/{id}/chats?limit=100` | — | All dialogs. |
| `GET` | `/accounts/{id}/channels?limit=100` | — | Channels only. |
| `GET` | `/accounts/{id}/groups?limit=100` | — | Groups only. |
| `POST` | `/accounts/{id}/send` | `{peer, message}` | Send message. `peer` = username / phone / id. |
| `POST` | `/accounts/{id}/visit` | `{peer, limit?}` | Mark read + pull recent messages. |
| `POST` | `/accounts/{id}/click-ads` | `{peer, limit?}` | Fetch sponsored messages in a channel and click them. |

---

## 8. End-to-end Examples

> Replace `$BASE` with your Render URL (or `http://localhost:8000`) and `$KEY` with your `MASTER_API_KEY`.

### Example A — login a fresh number

```bash
# 1. Start login
curl -X POST $BASE/accounts/new \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"api_id":123456,"api_hash":"abcd1234","phone":"+919999999999","name":"test1"}'

# -> { "account_id": "acc_abc123def456", "phone_code_hash": "...", "gist_synced": ["acc_abc123def456__meta.json"] }

# 2. Submit OTP
curl -X POST $BASE/accounts/acc_abc123def456/otp \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"code":"12345"}'

# -> { "status":"authorized", "user":{...}, "gist_synced":["acc_abc123def456__session.session","acc_abc123def456__meta.json"] }
# OR if 2FA enabled:
# -> { "status":"2fa_required" }

# 3. (if 2FA) Submit password
curl -X POST $BASE/accounts/acc_abc123def456/2fa \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"password":"my2faPassword"}'
```

### Example B — upload an existing session

```bash
# Option 1: single .session file (then also call /meta)
curl -X POST $BASE/accounts/acc_mysession1/upload \
  -H "X-API-Key: $KEY" \
  -F "file=@my_session.session"

curl -X POST $BASE/accounts/acc_mysession1/meta \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"api_id":123456,"api_hash":"abcd1234","phone":"+919999999999","name":"personal"}'

# Option 2: zip with session.session + meta.json
zip -j upload.zip my_session.session meta.json
curl -X POST $BASE/accounts/acc_mysession2/upload \
  -H "X-API-Key: $KEY" \
  -F "file=@upload.zip"
```

### Example C — list channels and click ads

```bash
# Wake the service
curl $BASE/ping

# List channels for an account
curl $BASE/accounts/acc_mysession1/channels \
  -H "X-API-Key: $KEY"

# Click sponsored ads in a channel
curl -X POST $BASE/accounts/acc_mysession1/click-ads \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"peer":"some_channel_username","limit":5}'

# Send a message
curl -X POST $BASE/accounts/acc_mysession1/send \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"peer":"some_username","message":"hello from api"}'
```

### Example D — verify gist persistence

```bash
# Check sync status
curl $BASE/sync/status -H "X-API-Key: $KEY"
# -> {"configured": true, "gist_id": "abc123...", "gist_url": "https://gist.github.com/abc123..."}

# Manually re-pull from gist (after Render sleep / restart)
curl -X POST $BASE/sync/pull -H "X-API-Key: $KEY"

# Manually push all local sessions to gist
curl -X POST $BASE/sync/push -H "X-API-Key: $KEY"
```

---

## 9. Security Checklist

- [ ] Set `MASTER_API_KEY` to a long random string (≥ 32 chars) in Render env vars.
- [ ] Always send `X-API-Key` header from your client.
- [ ] Use a **dedicated GitHub token** with **only** the `gist` scope — don't reuse your full-access token.
- [ ] Don't commit `.env`, `*.session`, or `data/` to git (already in `.gitignore`).
- [ ] Use HTTPS (Render gives you this automatically).
- [ ] Rotate `MASTER_API_KEY` and `GITHUB_GIST_TOKEN` if you suspect either leaked.
- [ ] **Never** put your Telegram `api_id`/`api_hash` in a public repo. Pass them as env vars or upload-time.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| 401 `Session not authorized` after upload | The `.session` file was for a different `api_id`/`api_hash`. They must match. |
| 400 `OTP sign-in failed: ...` | OTP expired or wrong. Restart login with `/accounts/new`. |
| Render service won't wake from sleep | Make sure the `/ping` URL is hit regularly (cron-job.org / UptimeRobot). |
| Sessions gone after Render sleep | Gist persistence not configured. Set `GITHUB_GIST_TOKEN` + `GIST_ID` env vars. |
| `gist_synced: []` in response | Gist not configured — check `GITHUB_GIST_TOKEN` and `GIST_ID` env vars. |
| 422 from GitHub when pushing | Filename had `/` in it — shouldn't happen with this version (uses `__` separator). |
| `ClickSponsoredMessageRequest` not found | Telethon version mismatch. The endpoint will still return ad URLs you can visit manually. |
| 500 internal_error | Check Render logs in dashboard. Most common: missing `api_id`/`api_hash` for the account. |
