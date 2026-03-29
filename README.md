# hardstop

macOS menu bar app that fires escalating screen-border animations before meetings. Built for ultrawide monitors where system notifications appear too far right to notice.

Works in two modes:
- **Manual** — set a one-off deadline ("catch bus at 4:55 PM") from the menu bar
- **Calendar** — polls Google Calendar and fires alerts automatically before every meeting

Both modes use the same configurable animation system.

---

## Install

```bash
git clone https://github.com/mariobollini/hardstop
cd hardstop
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
hardstop
```

The app starts immediately in manual mode. A stop-sign icon appears in the menu bar.

---

## Calendar sync

### Option A — ICS feed (no setup, works with any calendar)

Open **Edit Config** from the menu bar → **Google Calendar Settings** → paste one or more ICS feed URLs, one per line.

Where to get your ICS URL:
- **Google Calendar**: Settings → click the calendar name → "Secret address in iCal format" → copy the link
- **Outlook / Microsoft 365**: Calendar → Share → Publish → ICS link
- **Apple Calendar / iCloud**: right-click calendar → Share Calendar → enable public calendar → copy URL

ICS feeds are polled every 60 seconds alongside any Google Calendar API connection.

---

### Option B — Google Calendar API (optional)

### Step 1 — Google Cloud credentials (one time, free)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (name it anything — "hardstop" works)
3. **APIs & Services → Enable APIs** → search "Google Calendar API" → Enable
4. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop app**
   - Name: anything
5. You'll see a **Client ID** and **Client Secret** — copy both

### Step 2 — Paste into hardstop.py

Open `hardstop.py` and find these two lines near the top (around line 27):

```python
BUNDLED_CLIENT_ID     = ""
BUNDLED_CLIENT_SECRET = ""
```

Replace the empty strings with your values:

```python
BUNDLED_CLIENT_ID     = "123456789-abc.apps.googleusercontent.com"
BUNDLED_CLIENT_SECRET = "GOCSPX-yourSecretHere"
```

### Step 3 — Connect

Restart hardstop. Open **Edit Config** from the menu bar, go to **Google Calendar Settings**, and click **Connect Google Calendar**. A browser window opens, you sign in with your Google account, grant calendar read access, and you're done. The token is saved locally and auto-refreshed forever.

> **Note:** The client secret for a Desktop app OAuth flow is not sensitive — it's a public app identifier that can't be kept secret in any installed app. This is a documented Google pattern used by every open source desktop app that accesses Google APIs.

---

## Configuration

`~/.hardstop/config.yaml` controls alert timing and animations. See [`config.example.yaml`](config.example.yaml) for all options with comments.

Easier: open **Edit Config** from the menu bar for a visual editor.

### Alert effects

| Effect | Description |
|--------|-------------|
| `normal` | Solid color border, optional blink |
| `expand` | Border grows inward with gradient fade |
| `snake` | Nokia Snake spiral crawls inward from the corner |
| `game_over` | Full-screen retro "GAME OVER" panel |
| `none` | Level disabled — no animation fires |

### Popup positions

| Position | Behavior |
|----------|----------|
| `top` / `bottom` / `center` | Info banner at that screen position |
| `none` | No banner; click the border to snooze (if higher levels exist) or dismiss |

---

## Manual hardstop

**Set Hardstop…** in the menu bar lets you pick a time or duration. The same alert sequence fires before that deadline. Only one manual hardstop is active at a time; setting a new one replaces the old.

---

## Open at login

Toggle **Open at Login** in the menu bar. This installs a LaunchAgent at `~/Library/LaunchAgents/com.hardstop.plist`.

Log output goes to `~/.hardstop/hardstop.log`.

---

## Data locations

| Path | Purpose |
|------|---------|
| `~/.hardstop/config.yaml` | Alert timing and animation config |
| `~/.hardstop/token.json` | Google OAuth token (auto-generated, never commit) |
| `~/.hardstop/hardstop.json` | Active manual hardstop time |
| `~/.hardstop/hardstop.log` | Log output when running via LaunchAgent |
