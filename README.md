# hardstop

macOS menu bar app that fires escalating screen-border animations before meetings. Built for ultrawide monitors where system notifications appear too far right to notice.

Works in two modes:
- **Manual** — set a one-off deadline ("catch bus at 4:55 PM") from the menu bar
- **Calendar** — polls Google Calendar and fires alerts automatically before every meeting

Both modes use the same configurable animation system.

---

## Install

```bash
git clone https://github.com/mariobellini/hardstop
cd hardstop
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
hardstop
```

The app starts immediately in manual mode. A stop-sign icon appears in the menu bar.

---

## Google Calendar setup (optional)

You need a free Google Cloud Project. No billing required — the Calendar API free quota (1 million requests/day) is far more than this app will ever use.

### One-time setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (name it anything — "hardstop" works)
3. **APIs & Services → Enable APIs** → search "Google Calendar API" → Enable
4. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop app**
   - Name: anything
5. Copy the **Client ID** and **Client Secret** from the credentials page

### Add to config

Edit `~/.hardstop/config.yaml` (created automatically on first run):

```yaml
oauth:
  client_id:     "YOUR_CLIENT_ID.apps.googleusercontent.com"
  client_secret: "YOUR_CLIENT_SECRET"
```

Then click **Authorize Google Calendar** in the menu bar. A browser window opens, you sign in with your Google account, grant calendar read access, and you're done. The token is saved locally and auto-refreshed.

---

## Configuration

`~/.hardstop/config.yaml` controls alert timing and animations. See [`config.example.yaml`](config.example.yaml) for all options with comments.

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
| `~/.hardstop/token.json` | Google OAuth token (auto-generated) |
| `~/.hardstop/hardstop.json` | Active manual hardstop time |
| `~/.hardstop/hardstop.log` | Log output when running via LaunchAgent |
