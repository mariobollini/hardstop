#!/usr/bin/env python3
"""
hardstop — Google Calendar & deadline overlay for ultrawide monitors.

Runs as a macOS menu bar app. Polls Google Calendar and lets you set manual
"hardstop" deadlines. Both trigger escalating screen-border animations so you
can't miss a meeting even on a giant ultrawide.

Setup:
  pip install -e .
  hardstop   # starts in manual mode; add Google credentials to ~/.hardstop/config.yaml for calendar sync
"""

import json
import math
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Paths & constants ────────────────────────────────────────────────────────

# ── Google OAuth credentials ──────────────────────────────────────────────────
# Create a free Google Cloud project, enable the Calendar API, then create an
# OAuth 2.0 Client ID (type: Desktop app) at console.cloud.google.com.
# Paste the two values here — this is the only setup step required.
BUNDLED_CLIENT_ID     = ""
BUNDLED_CLIENT_SECRET = ""

APP_DIR            = Path.home() / ".hardstop"
CONFIG_PATH        = APP_DIR / "config.yaml"
HARDSTOP_PATH      = APP_DIR / "hardstop.json"
TOKEN_PATH         = APP_DIR / "token.json"
CLIENT_SECRET_PATH = APP_DIR / "client_secret.json"  # legacy fallback
LAUNCH_AGENT_LABEL = "com.hardstop"
LAUNCH_AGENT_PATH  = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
CALENDAR_SCOPES    = ["https://www.googleapis.com/auth/calendar.readonly"]

# Float above all apps and full-screen spaces
_OVERLAY_LEVEL = 1001

DEFAULT_CONFIG = {
    "calendars": [],
    "popup_font": "retro",
    "popup_pos":  "center",   # global fallback
    "default_name": "You have a hard stop!",
    "alerts": [
        {
            "minutes_before": 5,
            "color": "#FFCC00",
            "width": 40,
            "blink_hz": 0.5,
            "effect": "fade",
            "snake_speed": 300,
            "popup_pos": "center",
        },
        {
            "minutes_before": 2,
            "color": "#FF6600",
            "width": 80,
            "blink_hz": 2.0,
            "effect": "expand",
            "snake_speed": 300,
            "popup_pos": "center",
        },
        {
            "minutes_before": 0,
            "color": "#FF0000",
            "width": 120,
            "blink_hz": 4.0,
            "effect": "game_over",
            "snake_speed": 300,
            "popup_pos": "center",
        },
    ],
}

# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    import yaml

    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            yaml.dump(DEFAULT_CONFIG, default_flow_style=False, sort_keys=False)
        )
        print(f"Created default config at {CONFIG_PATH}")

    try:
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        cfg = {**DEFAULT_CONFIG, **data}
        # Migrate old flags to unified effect field
        for alert in cfg.get("alerts", []):
            if "effect" not in alert:
                if alert.get("game_over"):
                    alert["effect"] = "game_over"
                elif alert.get("snake_mode"):
                    alert["effect"] = "snake"
                elif alert.get("expand"):
                    alert["effect"] = "expand"
                elif alert.get("gradient"):
                    alert["effect"] = "fade"
                else:
                    alert["effect"] = "normal"
            # Migrate: add per-alert popup_pos if missing
            if "popup_pos" not in alert:
                alert["popup_pos"] = cfg.get("popup_pos", "center")
        return cfg
    except Exception as e:
        print(f"Config load error: {e} — using defaults.")
        return DEFAULT_CONFIG.copy()


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


def _popup_accent_rgb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """If the configured color is near-black, use red for popup accents so they remain visible."""
    if r + g + b < 0.18:   # ~6% brightness threshold
        return 1.0, 0.0, 0.0
    return r, g, b


# ── LaunchAgent (Open at Login) ──────────────────────────────────────────────

def _login_item_enabled() -> bool:
    return LAUNCH_AGENT_PATH.exists()


def _set_login_item(enabled: bool) -> None:
    if enabled:
        LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        script = Path(sys.argv[0]).resolve()
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>{APP_DIR}/hardstop.log</string>
  <key>StandardErrorPath</key><string>{APP_DIR}/hardstop.log</string>
</dict>
</plist>
"""
        LAUNCH_AGENT_PATH.write_text(plist)
        subprocess.run(["launchctl", "load", str(LAUNCH_AGENT_PATH)], check=False)
    else:
        if LAUNCH_AGENT_PATH.exists():
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], check=False)
            LAUNCH_AGENT_PATH.unlink()


# ── Menu bar icon (stop-sign octagon) ───────────────────────────────────────

def _make_octagon_icon_colored():
    """64×64 red filled octagon for use in NSAlert dialogs."""
    from AppKit import NSImage, NSBezierPath, NSColor
    W, H = 64.0, 64.0
    cx, cy = W / 2, H / 2
    r = W / 2 - 4
    image = NSImage.alloc().initWithSize_((W, H))
    image.lockFocus()
    path = NSBezierPath.bezierPath()
    for i in range(8):
        angle = math.radians(22.5 + i * 45)
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        if i == 0:
            path.moveToPoint_((x, y))
        else:
            path.lineToPoint_((x, y))
    path.closePath()
    NSColor.colorWithRed_green_blue_alpha_(0.8, 0.13, 0.0, 1.0).set()
    path.fill()
    image.unlockFocus()
    return image


def _make_octagon_icon(filled: bool = False):
    from AppKit import NSImage, NSBezierPath, NSColor

    W, H = 18.0, 18.0
    cx, cy = W / 2, H / 2
    r = W / 2 - 1.5

    image = NSImage.alloc().initWithSize_((W, H))
    image.lockFocus()

    path = NSBezierPath.bezierPath()
    for i in range(8):
        angle = math.radians(22.5 + i * 45)
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        if i == 0:
            path.moveToPoint_((x, y))
        else:
            path.lineToPoint_((x, y))
    path.closePath()

    NSColor.colorWithWhite_alpha_(1.0, 1.0).set()
    if filled:
        path.fill()
    else:
        path.setLineWidth_(1.5)
        path.stroke()

    image.unlockFocus()
    image.setTemplate_(True)
    return image


# ── Google Calendar OAuth ────────────────────────────────────────────────────

_calendar_service = None
_calendar_lock = threading.Lock()


def _try_load_cached_token() -> bool:
    global _calendar_service
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        if not TOKEN_PATH.exists():
            return False
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), CALENDAR_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_PATH.write_text(creds.to_json())
            else:
                return False
        with _calendar_lock:
            _calendar_service = build("calendar", "v3", credentials=creds)
        print("Google Calendar: loaded cached credentials.")
        return True
    except Exception as e:
        print(f"Cached token load failed: {e}")
        return False


def _get_oauth_client_config() -> dict | None:
    """Return OAuth client config dict, or None if not configured.

    Uses BUNDLED_CLIENT_ID/SECRET constants; falls back to client_secret.json
    for legacy compatibility.
    """
    if BUNDLED_CLIENT_ID and BUNDLED_CLIENT_SECRET:
        return {
            "installed": {
                "client_id":     BUNDLED_CLIENT_ID,
                "client_secret": BUNDLED_CLIENT_SECRET,
                "redirect_uris": ["http://localhost"],
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }
        }

    if CLIENT_SECRET_PATH.exists():
        import json as _json
        return _json.loads(CLIENT_SECRET_PATH.read_text())

    return None


def authorize_calendar() -> bool:
    global _calendar_service
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print("Missing Google libraries. Run: pip install google-api-python-client google-auth-oauthlib")
        return False

    client_config = _get_oauth_client_config()
    if not client_config:
        print(
            "Google Calendar credentials not configured.\n"
            "Set BUNDLED_CLIENT_ID and BUNDLED_CLIENT_SECRET at the top of hardstop.py.\n"
            "Get credentials at: console.cloud.google.com → APIs & Services → Credentials\n"
            "(Create an OAuth 2.0 Client ID, type: Desktop app)\n"
            "\n"
            "Hardstop will continue running in manual-hardstop-only mode."
        )
        return False

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), CALENDAR_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_config(client_config, CALENDAR_SCOPES)
            creds = flow.run_local_server(port=0)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    with _calendar_lock:
        _calendar_service = build("calendar", "v3", credentials=creds)
    print("Google Calendar authorized.")
    return True


def _fetch_upcoming_events(calendars: list) -> list[tuple[str, str, datetime]]:
    with _calendar_lock:
        svc = _calendar_service
    if not svc:
        return []

    now = datetime.now(tz=timezone.utc)
    t_max = now + timedelta(hours=6)
    cal_ids = calendars if calendars else ["primary"]
    results = []

    for cal_id in cal_ids:
        try:
            resp = svc.events().list(
                calendarId=cal_id,
                timeMin=now.isoformat(),
                timeMax=t_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=10,
            ).execute()
            for event in resp.get("items", []):
                start = event.get("start", {})
                s = start.get("dateTime") or start.get("date")
                if not s:
                    continue
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                results.append((event["id"], event.get("summary", "Meeting"), dt))
        except Exception as e:
            print(f"Calendar fetch error ({cal_id}): {e}")

    return results


# ── Manual hardstop ──────────────────────────────────────────────────────────

def load_hardstop() -> tuple[datetime, str] | None:
    """Returns (datetime, name) or None."""
    if not HARDSTOP_PATH.exists():
        return None
    try:
        data = json.loads(HARDSTOP_PATH.read_text())
        dt = datetime.fromisoformat(data["time"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        name = data.get("name", "Hardstop")
        return dt, name
    except Exception:
        return None


def save_hardstop(dt: datetime, name: str = "Hardstop") -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    HARDSTOP_PATH.write_text(json.dumps({"time": dt.isoformat(), "name": name}))


def clear_hardstop() -> None:
    if HARDSTOP_PATH.exists():
        HARDSTOP_PATH.unlink()


def parse_hardstop_input(text: str) -> datetime | None:
    """
    Parse user input into a future datetime (timezone-aware, local time).

    Very permissive — accepts many formats:
      "4:55pm", "4:55 PM", "16:55", "455pm"  — time of day
      "30m", "30min", "30 mins", "30 minutes" — 30 minutes from now
      "5min", "5 min"                          — 5 minutes from now
      "1h", "1h30m", "1 hour 30 min"           — duration from now
      "45"                                     — bare integer = minutes from now
    """
    orig = text.strip()
    text = orig.lower().strip()

    # Normalise unit aliases (no \b at digit-letter boundary, e.g. "5min")
    text = re.sub(r"hours?", "h", text)
    text = re.sub(r"min(?:utes?|s)?", "m", text)
    text = re.sub(r"\s+", "", text)   # collapse all spaces

    now = datetime.now().astimezone()

    # Hours + minutes: "1h30m", "30m", "5m", "90m"
    m = re.fullmatch(r"(?:(\d+)h)?(\d+)m", text)
    if m:
        hours = int(m.group(1) or 0)
        mins  = int(m.group(2) or 0)
        if hours or mins:
            return now + timedelta(hours=hours, minutes=mins)

    # Hours only: "2h"
    m = re.fullmatch(r"(\d+)h", text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # Bare integer → minutes
    m = re.fullmatch(r"(\d+)", text)
    if m:
        mins = int(m.group(1))
        if mins > 0:
            return now + timedelta(minutes=mins)

    # Time of day — try a wide variety of formats on the normalised and original text
    for src in (text, orig.lower().replace(" ", "")):
        for fmt in ("%I:%M%p", "%H:%M", "%I%p", "%I:%M", "%I%M%p", "%H%M"):
            try:
                t = datetime.strptime(src, fmt)
                result = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                if result <= now:
                    result += timedelta(days=1)
                return result
            except ValueError:
                continue

    return None


# ── Alert scheduler ──────────────────────────────────────────────────────────

class AlertScheduler:
    def __init__(self, config: dict, on_alert):
        self._config = config
        # fn(event_id, label, start_dt, alert_cfg, all_alerts_desc)
        self._on_alert = on_alert
        self._fired: set = set()
        self._lock = threading.Lock()

    def suppress_event(self, event_id: str) -> None:
        """Mark all alert levels for this event as done (full dismiss)."""
        with self._lock:
            for alert in self._config.get("alerts", []):
                self._fired.add((event_id, alert["minutes_before"]))

    def reset_hardstop_alerts(self) -> None:
        with self._lock:
            self._fired = {k for k in self._fired if k[0] != "__hardstop__"}

    def poll(self) -> list[tuple[str, datetime]]:
        now = datetime.now(tz=timezone.utc)
        events = _fetch_upcoming_events(self._config.get("calendars", []))

        hs = load_hardstop()
        if hs:
            hs_dt, hs_name = hs
            if now > hs_dt + timedelta(minutes=5):
                clear_hardstop()
            else:
                events.append(("__hardstop__", hs_name, hs_dt))

        alerts_desc = sorted(
            self._config.get("alerts", []),
            key=lambda a: a["minutes_before"],
            reverse=True,  # [5min, 2min, 0min]
        )

        for event_id, label, start_dt in events:
            for alert in alerts_desc:
                if alert.get("effect") == "none":
                    continue  # level disabled — no alarm
                mins = alert["minutes_before"]
                target = start_dt - timedelta(minutes=mins)
                key = (event_id, mins)
                with self._lock:
                    if key in self._fired:
                        continue
                    if abs(now - target) <= timedelta(seconds=45):
                        self._fired.add(key)
                        self._on_alert(event_id, label, start_dt, alert, alerts_desc)

        upcoming = [
            (lbl, dt) for _, lbl, dt in events
            if dt > datetime.now(tz=timezone.utc)
        ]
        upcoming.sort(key=lambda x: x[1])
        return upcoming


# ── Border overlay view ──────────────────────────────────────────────────────

import objc
from Foundation import NSObject
from AppKit import NSView


class _BorderView(NSView):
    """
    Transparent full-screen view drawing a colored border around screen edges.
    Supports gradient-fade, solid, and snake-crawl animation modes.
    """

    def initWithFrame_(self, frame):
        self = objc.super(_BorderView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._cfg = None
        self._extra_width = 0.0
        self._snake_coverage = 0.0
        self._click_cb = None
        return self

    @objc.python_method
    def configure(self, alert_cfg: dict) -> None:
        self._cfg = alert_cfg
        self._extra_width = 0.0
        self._snake_coverage = 0.0
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_extra_width(self, w: float) -> None:
        self._extra_width = w
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_snake_coverage(self, coverage: float) -> None:
        self._snake_coverage = max(0.0, coverage)  # raw total, may exceed 1.0
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if not self._cfg:
            return

        from AppKit import NSColor, NSBezierPath
        from Foundation import NSMakeRect

        cfg = self._cfg
        r, g, b = _hex_to_rgb(cfg.get("color", "#FF0000"))
        w = cfg.get("width", 40) + self._extra_width
        fw = self.frame().size.width
        fh = self.frame().size.height
        color = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)
        effect = cfg.get("effect", "normal")

        if effect == "none":
            return
        elif effect == "snake":
            self._draw_snake(fw, fh, w, color)
        elif effect == "game_over":
            NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0).set()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, fw, fh))
        elif effect == "expand":
            self._draw_border_gradient(fw, fh, w, r, g, b)
        else:
            self._draw_border(fw, fh, w, color, cfg)

    @objc.python_method
    def _draw_border(self, fw, fh, w, color, cfg):
        from AppKit import NSBezierPath
        from Foundation import NSMakeRect

        # Flat solid border — no gradients
        color.set()
        for rect in [
            NSMakeRect(0,      fh - w, fw, w),   # top
            NSMakeRect(0,      0,      fw, w),   # bottom
            NSMakeRect(0,      0,      w,  fh),  # left
            NSMakeRect(fw - w, 0,      w,  fh),  # right
        ]:
            NSBezierPath.fillRect_(rect)

    @objc.python_method
    def _draw_border_gradient(self, fw, fh, w, r, g, b):
        """Gradient border: solid at screen edge fading to transparent inward (expand effect)."""
        from AppKit import NSGradient, NSColor
        from Foundation import NSMakeRect

        solid = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)
        clear = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.0)
        grad  = NSGradient.alloc().initWithColors_([solid, clear])

        # NSGradient angle: 0°=left→right, 90°=bottom→top, 180°=right→left, 270°=top→bottom
        grad.drawInRect_angle_(NSMakeRect(0,      fh - w, fw, w), 270.0)  # top strip
        grad.drawInRect_angle_(NSMakeRect(0,      0,      fw, w),  90.0)  # bottom strip
        grad.drawInRect_angle_(NSMakeRect(0,      0,      w,  fh),   0.0)  # left strip
        grad.drawInRect_angle_(NSMakeRect(fw - w, 0,      w,  fh), 180.0)  # right strip

    @objc.python_method
    def _draw_snake(self, fw, fh, w, color):
        """
        Continuous rectangular spiral drawn as ONE stroke path.
        Each ring uses a shortened top edge + vertical inward step to connect
        seamlessly to the next ring — no separate closed rects, no corner gaps.
        NSView coords: y=0 at bottom, clockwise = right↓ bottom← left↑ top→.
        """
        from AppKit import NSBezierPath, NSButtLineCapStyle, NSMiterLineJoinStyle

        gap        = 2.0
        ring_pitch = w + gap
        loop_count = int(self._snake_coverage)
        frac       = self._snake_coverage - loop_count

        path = NSBezierPath.bezierPath()
        path.setLineWidth_(w)
        path.setLineCapStyle_(NSButtLineCapStyle)
        path.setLineJoinStyle_(NSMiterLineJoinStyle)

        started = False

        for ring_idx in range(loop_count + 1):
            inset = ring_idx * ring_pitch
            hw    = w / 2.0 + inset
            if hw + w / 2.0 >= min(fw, fh) / 2.0:
                break

            is_partial = (ring_idx == loop_count)

            # Every ring — partial or complete — uses the same 5-segment layout:
            #   right↓, bottom←, left↑, top→(shortened by ring_pitch), step↓ inward
            # The step lands exactly at the next ring's top-right start, making the
            # spiral one unbroken continuous line with clean 90° corners.
            segs = [
                (fw - hw,             fh - hw,             fw - hw,              hw              ),  # right ↓
                (fw - hw,             hw,                  hw,                   hw              ),  # bottom ←
                (hw,                  hw,                  hw,                   fh - hw         ),  # left ↑
                (hw,                  fh - hw,             fw - hw - ring_pitch, fh - hw         ),  # top → (short)
                (fw - hw - ring_pitch, fh - hw,            fw - hw - ring_pitch, fh - hw - ring_pitch),  # step ↓
            ]
            seg_lengths = [math.hypot(ex - sx, ey - sy) for sx, sy, ex, ey in segs]
            total_len   = sum(seg_lengths)
            target_len  = (frac * total_len) if is_partial else total_len

            if target_len <= 0:
                break

            remaining = target_len
            for (sx, sy, ex, ey), seg_len in zip(segs, seg_lengths):
                if remaining <= 0:
                    break
                if not started:
                    path.moveToPoint_((sx, sy))
                    started = True
                if remaining >= seg_len:
                    path.lineToPoint_((ex, ey))
                    remaining -= seg_len
                else:
                    t = remaining / seg_len
                    path.lineToPoint_((sx + t * (ex - sx), sy + t * (ey - sy)))
                    remaining = 0

            if is_partial:
                break

        if started:
            color.set()
            path.stroke()

    @objc.python_method
    def snake_head_position(self) -> tuple:
        """Return (x, y) of the current snake tip in view coordinates."""
        fw = self.frame().size.width
        fh = self.frame().size.height
        w  = self._cfg.get("width", 40) if self._cfg else 40
        hw = w / 2
        segments = [
            (hw,      fh - hw, fw - hw, fh - hw),
            (fw - hw, fh - hw, fw - hw, hw),
            (fw - hw, hw,      hw,      hw),
            (hw,      hw,      hw,      fh - hw),
        ]
        seg_lengths = [math.hypot(ex - sx, ey - sy) for sx, sy, ex, ey in segments]
        perimeter   = sum(seg_lengths)
        target_len  = self._snake_coverage * perimeter
        remaining   = target_len
        for (sx, sy, ex, ey), seg_len in zip(segments, seg_lengths):
            if seg_len == 0:
                continue
            if remaining <= seg_len:
                t = remaining / seg_len
                return sx + t * (ex - sx), sy + t * (ey - sy)
            remaining -= seg_len
        return segments[-1][2], segments[-1][3]

    def mouseDown_(self, event):
        pass  # accept so mouseUp_ fires

    def mouseUp_(self, event):
        if self._click_cb:
            self._click_cb()

    def acceptsFirstMouse_(self, event):
        return True

    def isOpaque(self):
        return False


# ── Info banner view ─────────────────────────────────────────────────────────

class _BannerView(NSView):
    """
    Popup banner. Supports modern, retro, and game-over styles.
    Position (center / top / snake) is handled by OverlayController.
    """

    def initWithFrame_(self, frame):
        self = objc.super(_BannerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._label      = ""
        self._start_dt   = None
        self._cfg        = None
        self._popup_font = "retro"
        self._game_over       = False
        self._at_start        = False
        self._dismiss_pending = False
        self._snooze_cb  = None
        self._dismiss_cb = None
        self._snooze_rect  = None
        self._dismiss_rect = None
        return self

    @objc.python_method
    def configure(self, label: str, start_dt, alert_cfg: dict,
                  popup_font: str, game_over: bool,
                  snooze_cb, dismiss_cb) -> None:
        self._label      = label
        self._start_dt   = start_dt
        self._cfg        = alert_cfg
        self._popup_font = popup_font
        self._game_over       = game_over
        self._at_start        = alert_cfg.get("minutes_before", 0) == 0
        self._dismiss_pending = False
        self._snooze_cb  = snooze_cb
        self._dismiss_cb = dismiss_cb
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _countdown(self) -> str:
        if not self._start_dt:
            return ""
        secs = int((self._start_dt - datetime.now(tz=timezone.utc)).total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            return f"{m}:{s:02d}"
        return "NOW"

    @objc.python_method
    def _event_time(self) -> str:
        if not self._start_dt:
            return ""
        local = self._start_dt.astimezone()
        return local.strftime("%H:%M")

    @objc.python_method
    def _accent_color(self):
        from AppKit import NSColor
        r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF8C00")) if self._cfg else (1.0, 0.5, 0.0)
        return NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)

    def drawRect_(self, rect):
        self._draw_retro()

    @objc.python_method
    def _draw_retro(self):
        from AppKit import (
            NSColor, NSBezierPath, NSMutableAttributedString, NSAttributedString,
            NSFont, NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
            NSKernAttributeName, NSCenterTextAlignment, NSLeftTextAlignment,
        )
        from Foundation import NSMakeRect, NSMakeRange

        fw, fh = self.frame().size.width, self.frame().size.height
        pad = 20.0

        r_c, g_c, b_c = _hex_to_rgb(self._cfg.get("color", "#FF8C00")) if self._cfg else (1.0, 0.5, 0.0)
        r_c, g_c, b_c = _popup_accent_rgb(r_c, g_c, b_c)
        accent = NSColor.colorWithRed_green_blue_alpha_(r_c, g_c, b_c, 1.0)

        # Dark terminal background — sharp corners, accent border
        NSColor.colorWithRed_green_blue_alpha_(0.02, 0.02, 0.02, 0.97).set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, fw, fh))
        border = NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, fw, fh))
        border.setLineWidth_(2.0)
        accent.set()
        border.stroke()

        def retro_font(size):
            f = NSFont.fontWithName_size_("Press Start 2P", size)
            if f is None:
                f = NSFont.fontWithName_size_("Monaco", size)
            return f or NSFont.boldSystemFontOfSize_(size)

        # Button layout: single OK when game_over or at_start; else Dismiss(left) Snooze(right)
        single_btn = self._game_over or self._at_start
        btn_w, btn_h = 100.0, 50.0
        btn_gap = 10.0
        btn_y = (fh - btn_h) / 2.0

        if single_btn:
            ok_x = fw - pad - btn_w
            self._dismiss_rect = (ok_x, btn_y, btn_w, btn_h)
            self._snooze_rect  = None
            text_right = ok_x - 14.0
        else:
            snooze_x  = fw - pad - btn_w
            dismiss_x = snooze_x - btn_gap - btn_w
            self._snooze_rect  = (snooze_x,  btn_y, btn_w, btn_h)
            self._dismiss_rect = (dismiss_x, btn_y, btn_w, btn_h)
            text_right = dismiss_x - 14.0

        cps = NSMutableParagraphStyle.new(); cps.setAlignment_(NSCenterTextAlignment)
        lps = NSMutableParagraphStyle.new(); lps.setAlignment_(NSLeftTextAlignment)

        # Text row: countdown (accent) + event time + name (white)
        # In dismiss-pending state: show confirm message instead
        font_size  = 19.0
        line_h     = font_size + 8
        text_y     = (fh - line_h) / 2.0

        if self._dismiss_pending:
            confirm_text = "CLEARS ALL REMINDERS FOR THIS EVENT"
            confirm_font_size = font_size * 0.7
            line_h  = confirm_font_size + 4
            text_y  = (fh - line_h) / 2.0
            ms = NSMutableAttributedString.alloc().initWithString_attributes_(confirm_text, {
                NSFontAttributeName:            retro_font(confirm_font_size),
                NSForegroundColorAttributeName: accent,
                NSParagraphStyleAttributeName:  cps,
                NSKernAttributeName:            1.5,
            })
        else:
            countdown  = self._countdown()
            event_time = self._event_time()
            rest = f"  {event_time}  {self._label.upper()}" if event_time else f"  {self._label.upper()}"
            ms = NSMutableAttributedString.alloc().initWithString_attributes_(
                countdown + rest, {
                    NSFontAttributeName:            retro_font(font_size),
                    NSForegroundColorAttributeName: NSColor.whiteColor(),
                    NSParagraphStyleAttributeName:  lps,
                    NSKernAttributeName:            2.0,
                }
            )
            # Countdown in accent color for emphasis
            ms.addAttribute_value_range_(
                NSForegroundColorAttributeName, accent, NSMakeRange(0, len(countdown))
            )

        if self._dismiss_pending:
            # Center across full banner width (buttons still drawn separately)
            ms.drawInRect_(NSMakeRect(pad, text_y, fw - 2 * pad, line_h))
        else:
            ms.drawInRect_(NSMakeRect(pad, text_y, text_right - pad, line_h))

        btn_font_size = 14.0
        if single_btn:
            label = "OMW" if self._game_over else "OK"
            self._draw_btn(label, self._dismiss_rect, primary=True, dim=False, cps=cps,
                           font_size=btn_font_size, font_name="Monaco")
        else:
            dismiss_primary = self._dismiss_pending
            self._draw_btn("DISMISS", self._dismiss_rect, primary=dismiss_primary, dim=False, cps=cps,
                           font_size=btn_font_size, font_name="Monaco")
            self._draw_btn("SNOOZE",  self._snooze_rect,  primary=not dismiss_primary, dim=self._dismiss_pending, cps=cps,
                           font_size=btn_font_size, font_name="Monaco")

    @objc.python_method
    def _draw_btn(self, title, btn_rect, primary, cps, font_size=14, font_name=None, dim=False):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSForegroundColorAttributeName, NSFontAttributeName,
            NSParagraphStyleAttributeName, NSKernAttributeName,
        )
        from Foundation import NSMakeRect

        bx, by, bw, bh = btn_rect
        r_c, g_c, b_c = _hex_to_rgb(self._cfg.get("color", "#FF8C00")) if self._cfg else (1, 0, 0)
        r_c, g_c, b_c = _popup_accent_rgb(r_c, g_c, b_c)

        if dim:
            # Deprioritized: very faint background, barely-visible border
            NSColor.colorWithWhite_alpha_(1.0, 0.03).set()
            NSBezierPath.fillRect_(NSMakeRect(bx, by, bw, bh))
            NSColor.colorWithWhite_alpha_(1.0, 0.10).set()
        elif primary:
            NSColor.colorWithRed_green_blue_alpha_(r_c, g_c, b_c, 0.35).set()
            NSBezierPath.fillRect_(NSMakeRect(bx, by, bw, bh))
            NSColor.colorWithRed_green_blue_alpha_(r_c, g_c, b_c, 0.7).set()
        else:
            NSColor.colorWithWhite_alpha_(1.0, 0.10).set()
            NSBezierPath.fillRect_(NSMakeRect(bx, by, bw, bh))
            NSColor.colorWithWhite_alpha_(1.0, 0.25).set()

        btn_border = NSBezierPath.bezierPathWithRect_(NSMakeRect(bx, by, bw, bh))
        btn_border.setLineWidth_(1.0)
        btn_border.stroke()

        if font_name:
            font = NSFont.fontWithName_size_(font_name, font_size) or NSFont.systemFontOfSize_(font_size)
        else:
            font = NSFont.systemFontOfSize_(font_size)

        text_alpha = 0.25 if dim else 0.95
        line_h = font_size + 2
        NSAttributedString.alloc().initWithString_attributes_(title, {
            NSFontAttributeName:            font,
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(text_alpha, 1.0),
            NSParagraphStyleAttributeName:  cps,
            NSKernAttributeName:            1.5,
        }).drawInRect_(NSMakeRect(bx + 2, by + (bh - line_h) / 2.0 + 1, bw - 4, line_h))

    def mouseDown_(self, event):
        pass  # accept so mouseUp_ fires

    def mouseUp_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        lx, ly = loc.x, loc.y

        def _hit(r):
            if r is None:
                return False
            bx, by, bw, bh = r
            return bx <= lx <= bx + bw and by <= ly <= by + bh

        if _hit(self._dismiss_rect) and self._dismiss_cb:
            if self._game_over or self._at_start:
                # Single click dismisses (OK button)
                self._dismiss_cb()
            elif self._dismiss_pending:
                # Second click confirms dismiss
                self._dismiss_cb()
            else:
                # First click: show confirm text
                self._dismiss_pending = True
                self.setNeedsDisplay_(True)
        elif _hit(self._snooze_rect) and self._snooze_cb:
            self._dismiss_pending = False
            self._snooze_cb()
        elif self._game_over and self._dismiss_cb:
            # Any click anywhere on the game_over banner also dismisses
            self._dismiss_cb()
        elif self._dismiss_pending:
            # Click elsewhere cancels pending dismiss
            self._dismiss_pending = False
            self.setNeedsDisplay_(True)

    def acceptsFirstMouse_(self, event):
        return True

    def isOpaque(self):
        return False


# ── Overlay controller ───────────────────────────────────────────────────────

class OverlayController:
    """
    Manages the border window and info banner.
    All public methods must be called on the main (AppKit) thread.
    """

    def __init__(self):
        self._border_win  = None
        self._border_view = None
        self._banner_win  = None
        self._banner_view = None
        self._timer       = None
        self._start_time  = 0.0
        self._current_cfg: dict | None = None
        self._label       = ""
        self._start_dt    = None
        self._event_id    = ""
        self._all_alerts: list = []   # sorted minutes_before descending
        self._dismiss_cb  = None
        self._tick_target = None
        self._popup_pos       = "center"
        self._snake_coverage  = 0.0
        self._last_tick_t     = 0.0
        self._auto_dismiss_after = None
        self._auto_dismiss_fn    = None   # callable used by tick(); None → self.dismiss

    @property
    def is_active(self) -> bool:
        return self._current_cfg is not None

    def show(self, event_id: str, label: str, start_dt, alert_cfg: dict,
             all_alerts: list, tick_target, dismiss_cb) -> None:
        """Display overlay. Replaces any existing overlay."""
        self._teardown()
        self._event_id    = event_id
        self._label       = label
        self._start_dt    = start_dt
        self._current_cfg = alert_cfg
        self._all_alerts  = all_alerts  # [5min, 2min, 0min]
        self._dismiss_cb  = dismiss_cb
        self._tick_target = tick_target
        self._start_time     = time.time()
        self._snake_coverage = 0.0
        self._last_tick_t    = time.time()

        # Auto-dismiss: game_over always; "none" popup_pos always
        effect    = alert_cfg.get("effect", "normal")
        popup_lvl = alert_cfg.get("popup_pos", "center")
        if effect == "game_over" or popup_lvl == "none":
            self._auto_dismiss_after = 120.0
        else:
            self._auto_dismiss_after = None

        from AppKit import NSScreen
        frame = NSScreen.mainScreen().frame()
        self._make_border(frame, alert_cfg)
        self._make_banner(frame, label, start_dt, alert_cfg)

        # When there's no popup, clicks on the border overlay act as snooze (if a
        # more-urgent level follows) or summon the banner (if already at max urgency).
        if popup_lvl == "none" and self._border_win and self._border_view:
            self._border_win.setIgnoresMouseEvents_(False)
            if self._has_higher_alert():
                self._border_view._click_cb = self._close_no_suppress
                self._auto_dismiss_fn = self._close_no_suppress
            else:
                self._border_view._click_cb = self._show_banner_on_click
                self._auto_dismiss_fn = self.dismiss

        from Foundation import NSTimer
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30.0, tick_target, "overlayTick:", None, True
        )

    def _has_higher_alert(self) -> bool:
        """True if there is a more-urgent (lower minutes_before) active level after the current one."""
        active = [a for a in self._all_alerts if a.get("effect") != "none"]
        try:
            idx = next(i for i, a in enumerate(active)
                       if a["minutes_before"] == self._current_cfg["minutes_before"])
            return idx + 1 < len(active)
        except StopIteration:
            return False

    def _show_banner_on_click(self) -> None:
        """Called when the border is clicked at the most-urgent no-popup level.
        Makes the border click-through again and shows the dismiss banner."""
        if self._banner_win:
            return  # banner already visible
        # Stop catching border clicks now that the banner is about to appear
        if self._border_win:
            self._border_win.setIgnoresMouseEvents_(True)
        if self._border_view:
            self._border_view._click_cb = None
        from AppKit import NSScreen
        frame = NSScreen.mainScreen().frame()
        # Force center popup regardless of config (we're summoning it on demand)
        forced_cfg = {**self._current_cfg, "popup_pos": "center"}
        self._make_banner(frame, self._label, self._start_dt, forced_cfg)

    def snooze(self) -> None:
        """Advance to the next more-urgent alert level. Dismiss if already at max."""
        try:
            idx = next(
                i for i, a in enumerate(self._all_alerts)
                if a["minutes_before"] == self._current_cfg["minutes_before"]
            )
        except StopIteration:
            idx = -1

        next_idx = idx + 1
        if next_idx < len(self._all_alerts):
            self.show(
                self._event_id, self._label, self._start_dt,
                self._all_alerts[next_idx], self._all_alerts,
                self._tick_target, self._dismiss_cb,
            )
        else:
            # Already at most urgent level — snooze = dismiss
            self.dismiss()

    def _close_no_suppress(self) -> None:
        """Tear down the current overlay without calling dismiss_cb.
        The scheduler will fire the next level naturally when its time comes."""
        self._teardown()

    def dismiss(self) -> None:
        cb = self._dismiss_cb
        self._teardown()
        if cb:
            cb()

    def _teardown(self) -> None:
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        for attr in ("_border_win", "_banner_win"):
            win = getattr(self, attr, None)
            if win:
                win.orderOut_(None)
        self._border_win  = None
        self._border_view = None
        self._banner_win  = None
        self._banner_view = None
        self._current_cfg = None

    def _make_border(self, frame, cfg) -> None:
        from AppKit import (
            NSWindow, NSBorderlessWindowMask, NSBackingStoreBuffered, NSColor,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
        )

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSBorderlessWindowMask, NSBackingStoreBuffered, False
        )
        win.setBackgroundColor_(NSColor.clearColor())
        win.setOpaque_(False)
        win.setIgnoresMouseEvents_(True)
        win.setLevel_(_OVERLAY_LEVEL)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )

        view = _BorderView.alloc().initWithFrame_(frame)
        view.configure(cfg)
        win.setContentView_(view)
        win.orderFrontRegardless()

        self._border_win  = win
        self._border_view = view

    def _make_banner(self, screen_frame, label, start_dt, cfg) -> None:
        from AppKit import (
            NSPanel, NSBorderlessWindowMask, NSBackingStoreBuffered, NSColor,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
        )
        from Foundation import NSMakeRect

        global_cfg  = load_config()
        popup_font  = global_cfg.get("popup_font", "retro")
        game_over   = cfg.get("effect", "normal") == "game_over"

        # Per-level popup_pos; fall back to global setting
        popup_pos = cfg.get("popup_pos") or global_cfg.get("popup_pos", "center")
        # Expand (walls closing in) always uses center — other positions fight the animation
        if cfg.get("effect") == "expand":
            popup_pos = "center"
        self._popup_pos = popup_pos

        # "none" → no banner (auto-dismiss handled by tick)
        if popup_pos == "none":
            return

        sw, sh = screen_frame.size.width, screen_frame.size.height

        bw = min(sw * 0.65, 860.0)
        bh = 96.0

        bx = (sw - bw) / 2
        if popup_pos == "top":
            by = sh - bh - 60
        elif popup_pos == "top-right":
            bx = sw - bw - 20
            by = sh - bh - 60
        else:  # center
            by = (sh - bh) / 2

        style = NSBorderlessWindowMask | 128  # NSNonactivatingPanelMask

        banner = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(bx, by, bw, bh), style, NSBackingStoreBuffered, False
        )
        banner.setBackgroundColor_(NSColor.clearColor())
        banner.setOpaque_(False)
        banner.setLevel_(_OVERLAY_LEVEL + 1)
        banner.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )

        view = _BannerView.alloc().initWithFrame_(NSMakeRect(0, 0, bw, bh))
        view.configure(label, start_dt, cfg, popup_font, game_over,
                       snooze_cb=self.snooze,
                       dismiss_cb=self.dismiss)
        banner.setContentView_(view)
        banner.orderFrontRegardless()

        self._banner_win  = banner
        self._banner_view = view

    def tick(self) -> None:
        """Called at 30 fps. Drives blink, expand, and snake animations."""
        if not self._border_win or not self._current_cfg:
            return

        cfg    = self._current_cfg
        t      = time.time()
        dt     = max(0.0, min(t - self._last_tick_t, 0.1)) if self._last_tick_t > 0 else 0.0
        self._last_tick_t = t

        # Auto-dismiss check
        if self._auto_dismiss_after is not None:
            if t - self._start_time >= self._auto_dismiss_after:
                (self._auto_dismiss_fn or self.dismiss)()
                return

        effect   = cfg.get("effect", "normal")
        blink_hz = cfg.get("blink_hz", 0)
        # Snake and Game Over are always fully opaque — blink doesn't apply
        alpha = (
            0.4 + 0.6 * abs(math.sin(math.pi * blink_hz * t))
            if blink_hz > 0 and effect not in ("snake", "game_over") else 1.0
        )
        self._border_win.setAlphaValue_(alpha)

        if effect == "snake":
            fw = self._border_view.frame().size.width
            fh = self._border_view.frame().size.height
            w  = cfg.get("width", 40)
            perimeter = 2 * (fw - w) + 2 * (fh - w)
            if perimeter > 0 and w > 0:
                gap        = 2.0
                ring_pitch = w + gap
                speed = cfg.get("snake_speed", 300)
                self._snake_coverage += dt * speed / perimeter
                # Cap at screen fill — never reset
                max_loops = max(1, int((min(fw, fh) / 2 - w / 2) / ring_pitch))
                if self._snake_coverage > max_loops:
                    self._snake_coverage = float(max_loops)
                self._border_view.set_snake_coverage(self._snake_coverage)

        elif effect == "expand":
            elapsed = t - self._start_time
            self._border_view.set_extra_width(elapsed * 24.0)  # 24 px/sec

        if self._banner_view:
            self._banner_view.setNeedsDisplay_(True)


# ── App delegate ─────────────────────────────────────────────────────────────

_app_delegate = None  # set at launch so Flask routes can dispatch alerts


class _AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _notif):
        global _app_delegate
        from AppKit import NSStatusBar, NSVariableStatusItemLength
        from Foundation import NSTimer

        _app_delegate = self
        self._config  = load_config()
        self._overlay = OverlayController()
        self._upcoming: list[tuple[str, datetime]] = []
        self._pending_alert: tuple | None = None
        self._icon_is_filled = False

        self._scheduler = AlertScheduler(self._config, self._on_alert_from_thread)
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=_try_load_cached_token, daemon=True).start()

        # Build icon images once; reuse by swapping references
        self._icon_empty  = _make_octagon_icon(filled=False)
        self._icon_filled = _make_octagon_icon(filled=True)

        bar  = NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        item.button().setImage_(self._icon_empty)
        item.button().setToolTip_("Hardstop")
        self._status_item = item  # retain

        self._build_menu()

        self._menu_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            30, self, "refreshMenuLabels:", None, True
        )

    @objc.python_method
    def _set_icon(self, filled: bool) -> None:
        """Swap icon only when state actually changes — avoids redundant redraws."""
        if filled == self._icon_is_filled:
            return
        self._icon_is_filled = filled
        self._status_item.button().setImage_(
            self._icon_filled if filled else self._icon_empty
        )

    # ── Menu ────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        from AppKit import NSMenu, NSMenuItem, NSControlStateValueOn, NSControlStateValueOff

        menu = NSMenu.new()

        self._next_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            self._next_event_label(), None, ""
        )
        self._next_item.setEnabled_(False)
        menu.addItem_(self._next_item)

        # Active hardstop row (hidden when none set)
        self._hs_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "", "clearHardstop:", ""
        )
        self._hs_item.setTarget_(self)
        menu.addItem_(self._hs_item)
        self._refresh_hardstop_item()

        menu.addItem_(NSMenuItem.separatorItem())

        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Set Hardstop…", "setHardstop:", "")
        mi.setTarget_(self)
        menu.addItem_(mi)

        menu.addItem_(NSMenuItem.separatorItem())

        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit Config…", "editConfig:", "")
        mi.setTarget_(self)
        menu.addItem_(mi)

        self._auth_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            self._auth_label(), "authorizeCalendar:", ""
        )
        self._auth_item.setTarget_(self)
        menu.addItem_(self._auth_item)

        menu.addItem_(NSMenuItem.separatorItem())

        self._login_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open at Login", "toggleLoginItem:", ""
        )
        self._login_item.setTarget_(self)
        self._login_item.setState_(
            1 if _login_item_enabled() else 0  # NSControlStateValueOn/Off
        )
        menu.addItem_(self._login_item)

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", ""
        )
        quit_item.setImage_(None)  # strip macOS Tahoe auto-symbol
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    @objc.python_method
    def _next_event_label(self) -> str:
        if self._upcoming:
            label, dt = self._upcoming[0]
            mins = max(0, int((dt - datetime.now(tz=timezone.utc)).total_seconds() / 60))
            return f"Next: {label} in {mins}m"
        return "No upcoming events"

    @objc.python_method
    def _auth_label(self) -> str:
        with _calendar_lock:
            has_svc = _calendar_service is not None
        return "Re-authorize Calendar" if has_svc else "Authorize Google Calendar"

    @objc.python_method
    def _refresh_hardstop_item(self) -> None:
        hs = load_hardstop()
        if hs:
            hs_dt, hs_name = hs
            time_str = hs_dt.astimezone().strftime("%-I:%M %p")
            self._hs_item.setTitle_(f"⏹ {hs_name}: {time_str}  —  Clear")
            self._hs_item.setEnabled_(True)
            self._hs_item.setHidden_(False)
        else:
            self._hs_item.setTitle_("")
            self._hs_item.setEnabled_(False)
            self._hs_item.setHidden_(True)

    def refreshMenuLabels_(self, _timer):
        self._next_item.setTitle_(self._next_event_label())
        self._refresh_hardstop_item()
        self._auth_item.setTitle_(self._auth_label())

    # ── Poll loop ────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while True:
            try:
                self._upcoming = self._scheduler.poll()
            except Exception as e:
                print(f"Poll error: {e}")
            time.sleep(60)

    # ── Alert routing (background thread → main thread) ──────────────────────

    def _on_alert_from_thread(self, event_id: str, label: str, start_dt,
                              alert_cfg: dict, all_alerts: list) -> None:
        self._pending_alert = (event_id, label, start_dt, alert_cfg, all_alerts)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showPendingAlert:", None, False
        )

    def showPendingAlert_(self, _) -> None:
        if not self._pending_alert:
            return
        event_id, label, start_dt, cfg, all_alerts = self._pending_alert
        self._pending_alert = None

        # Don't replace an active overlay with a less-urgent one
        if self._overlay.is_active and self._overlay._current_cfg:
            if cfg["minutes_before"] > self._overlay._current_cfg["minutes_before"]:
                return

        self._overlay.show(
            event_id, label, start_dt, cfg, all_alerts,
            tick_target=self,
            dismiss_cb=lambda: self._on_dismiss(event_id),
        )
        self._set_icon(True)

    @objc.python_method
    def _on_dismiss(self, event_id: str) -> None:
        if event_id == "__hardstop__":
            clear_hardstop()
            self._refresh_hardstop_item()
        else:
            self._scheduler.suppress_event(event_id)
        self._set_icon(False)

    def overlayTick_(self, _timer) -> None:
        self._overlay.tick()
        self._set_icon(self._overlay.is_active)

    # ── Menu actions ─────────────────────────────────────────────────────────

    def setHardstop_(self, _sender) -> None:
        from AppKit import NSAlert, NSTextField, NSView
        from Foundation import NSMakeRect

        alert = NSAlert.new()
        alert.setMessageText_("Hardstop")
        alert.setIcon_(_make_octagon_icon_colored())
        alert.addButtonWithTitle_("Set")
        alert.addButtonWithTitle_("Cancel")

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 58))

        time_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 32, 300, 24))
        time_field.setPlaceholderString_("When  —  4:55pm  ·  30m  ·  5min  ·  1h30m")

        name_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        name_field.setPlaceholderString_("Name  (optional)")

        container.addSubview_(time_field)
        container.addSubview_(name_field)
        alert.setAccessoryView_(container)
        alert.window().setInitialFirstResponder_(time_field)

        if alert.runModal() == 1000:  # NSAlertFirstButtonReturn
            default_name = self._config.get("default_name", "You have a hard stop!")
            name = name_field.stringValue().strip() or default_name
            time_text = time_field.stringValue().strip().lower()

            # Test shortcuts: "test1" / "test2" / "test3" → fire that alert level now
            m_test = re.match(r"^test(\d+)$", time_text)
            if m_test:
                level_idx = int(m_test.group(1)) - 1
                alerts_desc = sorted(
                    self._config.get("alerts", []),
                    key=lambda a: a["minutes_before"],
                    reverse=True,
                )
                if 0 <= level_idx < len(alerts_desc):
                    cfg = alerts_desc[level_idx]
                    start_dt = datetime.now(tz=timezone.utc)
                    label = name if name != "Hardstop" else f"Test Level {level_idx + 1}"
                    self._on_alert_from_thread(
                        f"__test_{level_idx}__", label, start_dt, cfg, alerts_desc,
                    )
                return

            dt = parse_hardstop_input(time_text)
            if dt:
                save_hardstop(dt, name)
                print(f"Hardstop '{name}' set for {dt.astimezone().strftime('%-I:%M %p')}")
                self._refresh_hardstop_item()
                self._scheduler.reset_hardstop_alerts()
            else:
                err = NSAlert.new()
                err.setMessageText_("Couldn't parse the time. Try '4:55pm', '30m', '1h30min'.")
                err.runModal()

    def clearHardstop_(self, _sender) -> None:
        clear_hardstop()
        self._scheduler.reset_hardstop_alerts()
        self._refresh_hardstop_item()

    def editConfig_(self, _sender) -> None:
        if not CONFIG_PATH.exists():
            load_config()
        url = f"http://localhost:{_CONFIG_PORT}/config"
        if _port_in_use(_CONFIG_PORT):
            subprocess.run(["open", url], check=False)
            return
        if not getattr(self, "_config_server_started", False):
            _start_config_server()
            self._config_server_started = True
        threading.Timer(0.5, lambda: subprocess.run(["open", url], check=False)).start()

    def authorizeCalendar_(self, _sender) -> None:
        threading.Thread(target=authorize_calendar, daemon=True).start()

    def toggleLoginItem_(self, sender) -> None:
        enabled = not _login_item_enabled()
        _set_login_item(enabled)
        sender.setState_(1 if enabled else 0)


# ── Config web server ────────────────────────────────────────────────────────

_CONFIG_PORT = 7891


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_config_server() -> None:
    """Launch the Flask config server on a background daemon thread."""
    import threading
    t = threading.Thread(target=_run_config_server, daemon=True)
    t.start()


def _run_config_server() -> None:
    from flask import Flask, jsonify, request, Response
    from waitress import serve

    app = Flask(__name__)

    @app.get("/config")
    def serve_config_page():
        return Response(_CONFIG_HTML, mimetype="text/html",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def get_config():
        cfg = load_config()
        return jsonify(cfg)

    @app.post("/api/config")
    def post_config():
        import yaml
        data = request.get_json(force=True)
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                yaml.dump(data, default_flow_style=False, sort_keys=False)
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/ping")
    def ping():
        return jsonify({"ok": True})

    @app.get("/api/auth_status")
    def auth_status():
        with _calendar_lock:
            authorized = _calendar_service is not None
        has_credentials = bool(BUNDLED_CLIENT_ID and BUNDLED_CLIENT_SECRET) or CLIENT_SECRET_PATH.exists()
        return jsonify({"authorized": authorized, "has_credentials": has_credentials})

@app.post("/api/open_url")
    def open_url():
        url = request.get_json(force=True).get("url", "")
        if url.startswith("https://"):
            subprocess.Popen(["open", url])
        return jsonify({"ok": True})

    @app.post("/api/authorize")
    def do_authorize():
        threading.Thread(target=authorize_calendar, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/preview/<int:n>")
    def preview_alert(n):
        if _app_delegate is None:
            return jsonify({"ok": False, "error": "App not ready"}), 503
        cfg = load_config()
        alerts_desc = sorted(
            cfg.get("alerts", []),
            key=lambda a: a["minutes_before"],
            reverse=True,
        )
        level_idx = n - 1
        if not (0 <= level_idx < len(alerts_desc)):
            return jsonify({"ok": False, "error": "Level out of range"}), 400
        alert_cfg = alerts_desc[level_idx]
        start_dt = datetime.now(tz=timezone.utc)
        _app_delegate._on_alert_from_thread(
            f"__preview_{level_idx}__", f"Preview Level {n}",
            start_dt, alert_cfg, alerts_desc,
        )
        return jsonify({"ok": True})

    serve(app, host="127.0.0.1", port=_CONFIG_PORT, _quiet=True)


_CONFIG_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hardstop Config</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0c0c;--card:#131010;--border:#2a1515;
  --accent:#cc2200;--accent-hi:#ff4422;--accent-dim:#3a0a00;
  --text:#ddd0cf;--muted:#887070;--dim:#aa8888;--success:#44cc66;
}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;min-height:100vh}
header{position:sticky;top:0;z-index:100;background:#0e0808;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;padding:12px 20px}
.logo-svg{flex-shrink:0}
header h1{flex:1;font-size:14px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--text)}
header h1 span{color:var(--accent-hi)}
#save-btn{background:var(--accent);color:#fff;border:none;padding:7px 22px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.05em;transition:background .15s}
#save-btn:hover{background:var(--accent-hi)}
#save-btn:active{background:#991800}
main{max-width:960px;margin:0 auto;padding:20px 16px 60px}
.section-label{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}

/* alerts grid */
.alerts-grid{display:flex;flex-wrap:nowrap;gap:10px;overflow-x:auto;align-items:stretch;padding-bottom:4px;margin-bottom:18px;scrollbar-width:thin;scrollbar-color:var(--accent-dim) transparent}
.alerts-grid::-webkit-scrollbar{height:4px}
.alerts-grid::-webkit-scrollbar-thumb{background:var(--accent-dim)}
.alert-card{flex:1 1 0;min-width:220px;background:var(--card);border:1px solid var(--border);overflow:hidden;display:flex;flex-direction:column}
.alert-card:hover{border-color:#3d2020}

/* card parts */
.card-header{display:flex;align-items:center;gap:7px;padding:9px 10px;border-bottom:1px solid var(--border)}
.level-badge{color:var(--accent-hi);font-size:11px;font-weight:800;letter-spacing:.1em;padding:2px 7px;text-transform:uppercase;flex-shrink:0}
.f-mins-inline{background:#0a0505;border:1px solid var(--border);color:var(--text);padding:2px 4px;font-size:12px;text-align:center;outline:none;width:40px}
.f-mins-inline:focus{border-color:var(--accent)}
.f-mins-inline::-webkit-inner-spin-button,.f-mins-inline::-webkit-outer-spin-button{opacity:1;filter:invert(1) brightness(0.6)}
.mins-label{font-size:12px;color:var(--muted);white-space:nowrap;flex:1}
.preview-wrap{padding:8px 8px 0;background:#090505}
.preview-canvas{display:block;width:100%;background:#060303}
.card-fields{flex:1;padding:10px 10px 12px;display:flex;flex-direction:column;gap:9px}
.field-row{display:flex;flex-direction:column;gap:4px}
.field-label{font-size:12px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
select.f-effect{background:#0a0505;border:1px solid var(--border);color:var(--text);padding:5px 8px;font-size:12px;width:100%;outline:none;cursor:pointer}
select.f-effect:focus{border-color:var(--accent)}
select.f-effect option{background:#0c0c0c}
.effect-note{font-size:10px;color:var(--muted);margin-top:-3px;line-height:1.4;display:none}
.two-col-row{display:flex;gap:8px}
.field-group{flex:1;display:flex;flex-direction:column;gap:4px}
.disc-btns{display:flex;gap:0}
.disc-btn{background:var(--accent-dim);border:1px solid var(--border);color:var(--muted);padding:5px 0;font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;flex:1;text-align:center}
.disc-btn:not(:first-child){border-left:none}
.disc-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.disc-btn:hover:not(.active){background:#2a0e0e;color:var(--dim)}
.snake-fields{display:none}
.preview-btn{width:100%;background:none;border:1px solid #441111;color:#884444;padding:7px 0;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:auto}
.preview-btn:hover{border-color:var(--accent);color:var(--accent-hi);background:#110505}

/* disabled level (effect=none) */
.alert-card.level-disabled .card-fields>*:not(:first-child){opacity:.3;pointer-events:none}
/* popup locked (effect=expand: center only) */
.alert-card.popup-locked .f-popup-btn:not([data-val="center"]){opacity:.25;pointer-events:none}

/* themes */
.themes-list{display:flex;flex-direction:column;gap:3px}
.theme-row{display:flex;align-items:center;gap:10px;padding:6px 8px;border:1px solid transparent;cursor:pointer;transition:border-color .15s;user-select:none}
.theme-row:not(.theme-row-custom):hover{border-color:var(--accent)}
.theme-row.theme-row-custom{cursor:default}
.theme-swatches{display:flex;gap:5px;flex-shrink:0}
.theme-swatch{width:30px;height:30px;border:1px solid rgba(255,255,255,.12);position:relative;flex-shrink:0}
.theme-swatch.black-swatch::after{content:"";position:absolute;bottom:3px;right:3px;width:6px;height:6px;border-radius:50%;background:#FF0000}
.theme-swatch.custom-swatch{cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;color:rgba(255,255,255,.4)}
.theme-swatch.custom-swatch:hover{border-color:var(--accent);color:rgba(255,255,255,.8)}
.theme-label{font-size:12px;color:var(--muted);flex:1;letter-spacing:.04em}
/* color picker popup */
.cp-popup{position:fixed;z-index:9999;background:#140808;border:1px solid var(--accent);padding:10px;min-width:160px;display:none}
.cp-popup.open{display:block}
.cp-presets{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-bottom:8px}
.cp-preset{width:24px;height:24px;border:1px solid transparent;cursor:pointer}
.cp-preset:hover{border-color:#fff;transform:scale(1.1)}
.cp-hex-row{display:flex;align-items:center;gap:5px}
.cp-hash{color:var(--muted);font-size:13px;font-weight:700}
.cp-hex-input{background:#080303;border:1px solid var(--border);color:var(--text);padding:4px 6px;font-size:12px;font-family:"SF Mono",Menlo,monospace;width:72px;outline:none;letter-spacing:.08em}
.cp-hex-input:focus{border-color:var(--accent)}
.cp-swatch-preview{width:24px;height:24px;border:1px solid rgba(255,255,255,.2);flex-shrink:0}
.panel-wrap{background:var(--card);border:1px solid var(--border);margin-bottom:18px;overflow:hidden}
.panel-wrap summary{list-style:none;padding:10px 14px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);user-select:none;display:flex;align-items:center;gap:8px}
.panel-wrap summary::before{content:"▸";font-size:10px;color:var(--accent);transition:transform .2s}
.panel-wrap[open] summary::before{transform:rotate(90deg)}
.panel-wrap summary::-webkit-details-marker{display:none}
.panel-inner{padding:10px 14px 14px;display:flex;flex-direction:column;gap:12px}
.panel-row{display:flex;align-items:center;gap:12px}
.panel-row-label{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);min-width:80px}

/* calendar */
.cal-auth-row{display:flex;align-items:center;gap:10px;padding-bottom:12px;border-bottom:1px solid var(--border)}
.cal-auth-status{flex:1;font-size:13px;color:var(--muted)}
.cal-auth-status.ok{color:var(--success)}
.cal-auth-btn{background:var(--accent-dim);color:var(--dim);border:1px solid var(--border);padding:5px 12px;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:.05em;transition:all .15s}
.cal-auth-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
#cals-input{width:100%;background:#090404;border:1px solid var(--border);color:var(--text);padding:7px 10px;font-size:12px;font-family:"SF Mono",Menlo,monospace;outline:none;resize:vertical;min-height:44px}
#cals-input:focus{border-color:var(--accent)}
.cals-hint{font-size:11px;color:var(--muted);margin-top:4px}

/* toast */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(16px);background:#1a2a1a;border:1px solid #335533;color:var(--success);padding:9px 18px;font-size:13px;font-weight:600;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.err{background:#2a1a1a;border-color:#553333;color:#ff6644}
</style>
</head>
<body>

<header>
  <svg class="logo-svg" width="26" height="26" viewBox="0 0 30 30">
    <polygon points="9,2 21,2 28,9 28,21 21,28 9,28 2,21 2,9" fill="#cc2200" stroke="#ff4422" stroke-width="1"/>
  </svg>
  <h1><span>Hardstop</span> Config</h1>
  <button id="save-btn">Save</button>
</header>

<main>
  <details class="panel-wrap" id="alerts-panel" open>
    <summary>Alert Levels</summary>
    <div class="panel-inner" style="padding-top:8px">
      <div id="alerts-grid" class="alerts-grid"></div>
    </div>
  </details>

  <details class="panel-wrap" id="customizations-panel" open>
    <summary>Customizations</summary>
    <div class="panel-inner">
      <div class="section-label" style="margin-bottom:6px">Themes</div>
      <div class="themes-list" id="themes-list"></div>
      <div class="panel-row">
        <div class="panel-row-label">Default name</div>
        <input id="default-name-input" type="text" placeholder="You have a hard stop!"
          style="flex:1;background:#090404;border:1px solid var(--border);color:var(--text);padding:5px 8px;font-size:12px;outline:none">
      </div>
    </div>
  </details>

  <details class="panel-wrap">
    <summary>Google Calendar Settings</summary>
    <div class="panel-inner">
      <div class="cal-auth-row">
        <span class="cal-auth-status" id="cal-auth-status">Checking…</span>
        <span id="cal-auth-actions" style="display:flex;gap:6px;align-items:center"></span>
      </div>
      <div>
        <div class="field-label" style="margin-bottom:6px">Calendar IDs</div>
        <textarea id="cals-input" rows="2"
          placeholder="Leave empty for primary calendar. One calendar ID per line."></textarea>
        <div class="cals-hint">Find your calendar ID in Google Calendar → Settings → Integrate calendar</div>
      </div>
    </div>
  </details>
</main>

<div id="toast"></div>

<script>
const REF_W = 1920, REF_H = 1080;

const DEFAULT_ALERT = {
  minutes_before:5, color:"#FFCC00",
  width:40, blink_hz:0.5,
  effect:"fade",
  snake_speed:300, popup_pos:"center",
};

// Preset themes: colors applied to alerts sorted highest→lowest minutes_before.
// "black" levels store #000000; popup auto-switches to red accent when color is near-black.
const THEME_PRESETS = [
  { label:"Yellow / Orange / Red",  colors:["#FFCC00","#FF6600","#FF0000"] },
  { label:"Yellow / Red / Red",     colors:["#FFCC00","#FF0000","#FF0000"] },
  { label:"Yellow / Red / Black",   colors:["#FFCC00","#FF0000","#000000"] },
  { label:"Yellow / Black / Black", colors:["#FFCC00","#000000","#000000"] },
];

const CP_PRESETS=["#FFCC00","#FF8800","#FF4400","#FF0000","#FF44BB","#AA22FF","#88DDFF","#44FF88","#CCFF44","#FFFFFF"];

let state = { calendars:[], alerts:[], popup_font:"retro", popup_pos:"center", default_name:"You have a hard stop!" };
let rafs = {};

function nearestDisc(btns, val) {
  let best=btns[0], bestDist=Infinity;
  btns.forEach(b=>{const d=Math.abs(parseFloat(b.dataset.val)-val);if(d<bestDist){bestDist=d;best=b;}});
  return best;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    state.calendars    = cfg.calendars    || [];
    state.alerts       = cfg.alerts       || [];
    state.popup_font   = cfg.popup_font   || "retro";
    state.popup_pos    = cfg.popup_pos    || "center";
    state.default_name = cfg.default_name || "You have a hard stop!";
    state.alerts.forEach(a => {
      if (!a.effect) {
        if (a.game_over)    a.effect="game_over";
        else if (a.snake_mode) a.effect="snake";
        else if (a.expand)  a.effect="expand";
        else if (a.gradient) a.effect="fade";
        else a.effect="normal";
      }
      if (!a.popup_pos) a.popup_pos = state.popup_pos;
    });
    document.getElementById("cals-input").value = state.calendars.join("\n");
    document.getElementById("default-name-input").value = state.default_name;
    renderAlerts();
    initThemes();
  } catch(e) { toast("Failed to load config: "+e, true); }
  checkAuthStatus();
}

document.getElementById("default-name-input").addEventListener("input", e => {
  state.default_name = e.target.value.trim() || "You have a hard stop!";
});

// ── Calendar auth ──────────────────────────────────────────────────────────────
async function checkAuthStatus() {
  try {
    const d = await (await fetch("/api/auth_status")).json();
    const el=document.getElementById("cal-auth-status");
    const actions=document.getElementById("cal-auth-actions");
    actions.innerHTML="";

    if (d.authorized) {
      el.textContent="✓ Google Calendar connected"; el.className="cal-auth-status ok";
      const reauth=document.createElement("button"); reauth.className="cal-auth-btn"; reauth.textContent="Re-authorize";
      reauth.addEventListener("click", triggerAuth);
      actions.appendChild(reauth);
    } else if (d.has_credentials) {
      el.textContent="Not connected"; el.className="cal-auth-status";
      const btn=document.createElement("button"); btn.className="cal-auth-btn"; btn.textContent="Connect Google Calendar";
      btn.addEventListener("click", triggerAuth);
      actions.appendChild(btn);
    } else {
      el.textContent="Google Calendar not configured — running in manual mode"; el.className="cal-auth-status";
    }
  } catch(e) {}
}

async function triggerAuth() {
  try { await fetch("/api/authorize",{method:"POST"}); toast("Browser will open for Google Calendar authorization"); setTimeout(checkAuthStatus,8000); }
  catch(e) { toast("Authorization failed: "+e,true); }
}

// ── Themes ────────────────────────────────────────────────────────────────────
let _openPicker=null; // currently open color picker element

function applyThemeColors(colors){
  const sorted=[...state.alerts].map((a,i)=>({...a,_i:i})).sort((a,b)=>b.minutes_before-a.minutes_before);
  sorted.forEach((a,pos)=>{ if(pos<colors.length) state.alerts[a._i].color=colors[pos]; });
  renderAlerts();
  renderThemes();
}

function sortedAlertColors(){
  return [...state.alerts].map((a,i)=>({...a,_i:i}))
    .sort((a,b)=>b.minutes_before-a.minutes_before)
    .map(a=>a.color||"#FF0000");
}

function makeColorPicker(swatchEl, alertIdx){
  // Picker is appended to body (position:fixed) so panel overflow:hidden can't clip it
  const popup=document.createElement("div"); popup.className="cp-popup";
  popup.style.position="fixed";
  const presetsDiv=document.createElement("div"); presetsDiv.className="cp-presets";
  CP_PRESETS.forEach(c=>{
    const p=document.createElement("div"); p.className="cp-preset"; p.style.background=c;
    p.addEventListener("mousedown",e=>{e.preventDefault(); e.stopPropagation(); applyCustomColor(alertIdx,c,swatchEl,hexInput,preview); });
    presetsDiv.appendChild(p);
  });
  popup.appendChild(presetsDiv);
  const hexRow=document.createElement("div"); hexRow.className="cp-hex-row";
  const hash=document.createElement("span"); hash.className="cp-hash"; hash.textContent="#";
  const hexInput=document.createElement("input"); hexInput.className="cp-hex-input"; hexInput.maxLength=6;
  hexInput.value=(state.alerts[alertIdx].color||"#FF0000").replace("#","");
  const preview=document.createElement("div"); preview.className="cp-swatch-preview";
  preview.style.background=state.alerts[alertIdx].color||"#FF0000";
  hexInput.addEventListener("input",()=>{
    const v=hexInput.value.replace(/[^0-9a-fA-F]/g,""); hexInput.value=v;
    if(v.length===6){ applyCustomColor(alertIdx,"#"+v,swatchEl,hexInput,preview); }
  });
  hexInput.addEventListener("click",e=>e.stopPropagation());
  hexRow.appendChild(hash); hexRow.appendChild(hexInput); hexRow.appendChild(preview);
  popup.appendChild(hexRow);
  popup.addEventListener("mousedown",e=>e.stopPropagation());
  document.body.appendChild(popup);
  return popup;
}

function applyCustomColor(alertIdx, color, swatchEl, hexInput, preview){
  state.alerts[alertIdx].color=color;
  swatchEl.style.background=color;
  hexInput.value=color.replace("#","");
  preview.style.background=color;
}

function renderThemes(){
  const list=document.getElementById("themes-list"); if(!list) return;
  // Remove any previously body-attached pickers before re-rendering
  document.querySelectorAll(".cp-popup").forEach(el=>el.remove());
  if(_openPicker) _openPicker=null;
  list.innerHTML="";
  // Preset rows
  THEME_PRESETS.forEach(theme=>{
    const row=document.createElement("div"); row.className="theme-row";
    const swatches=document.createElement("div"); swatches.className="theme-swatches";
    theme.colors.forEach((c,i)=>{
      const sq=document.createElement("div"); sq.className="theme-swatch";
      const dispColor=(theme.display&&theme.display[i]!=null)?theme.display[i]:c;
      sq.style.background=dispColor;
      if(theme.display&&theme.display[i]!=null) sq.classList.add("black-swatch");
      swatches.appendChild(sq);
    });
    const lbl=document.createElement("div"); lbl.className="theme-label"; lbl.textContent=theme.label;
    row.appendChild(swatches); row.appendChild(lbl);
    row.addEventListener("click",()=>applyThemeColors(theme.colors));
    list.appendChild(row);
  });
  // Custom row
  const customRow=document.createElement("div"); customRow.className="theme-row theme-row-custom";
  const customSwatches=document.createElement("div"); customSwatches.className="theme-swatches";
  const sorted=[...state.alerts].map((a,i)=>({...a,_i:i})).sort((a,b)=>b.minutes_before-a.minutes_before);
  sorted.slice(0,3).forEach(a=>{
    const sq=document.createElement("div"); sq.className="theme-swatch custom-swatch";
    sq.style.background=a.color||"#FF0000"; sq.title="Click to set color";
    sq.textContent="✎";
    const picker=makeColorPicker(sq,a._i);
    sq.addEventListener("click",e=>{
      e.stopPropagation();
      const isOpen=picker.classList.contains("open");
      if(_openPicker&&_openPicker!==picker){ _openPicker.classList.remove("open"); }
      if(!isOpen){
        // Position fixed relative to swatch, flip above if near bottom of viewport
        const r=sq.getBoundingClientRect();
        picker.style.left=r.left+"px";
        picker.style.top=(r.bottom+4)+"px";
        picker.style.bottom="";
        picker.classList.add("open");
        // After it's visible, check if it overflows the bottom
        requestAnimationFrame(()=>{
          const pr=picker.getBoundingClientRect();
          if(pr.bottom>window.innerHeight-8){
            picker.style.top=""; picker.style.bottom=(window.innerHeight-r.top+4)+"px";
          }
        });
        _openPicker=picker;
      } else {
        picker.classList.remove("open");
        _openPicker=null;
      }
    });
    customSwatches.appendChild(sq);
  });
  const customLbl=document.createElement("div"); customLbl.className="theme-label"; customLbl.textContent="Custom";
  customRow.appendChild(customSwatches); customRow.appendChild(customLbl);
  list.appendChild(customRow);
}

function initThemes(){
  renderThemes();
  // Close pickers when clicking outside
  document.addEventListener("click",()=>{ if(_openPicker){ _openPicker.classList.remove("open"); _openPicker=null; } });
}

// ── Render alerts ─────────────────────────────────────────────────────────────
function renderAlerts() {
  Object.values(rafs).forEach(cancelAnimationFrame); rafs={};
  const grid=document.getElementById("alerts-grid"); grid.innerHTML="";
  const order=[...state.alerts].map((a,i)=>({...a,_i:i})).sort((a,b)=>b.minutes_before-a.minutes_before);
  order.forEach((a,pos)=>{ const card=buildCard(a,a._i,pos+1); grid.appendChild(card); initPreview(card.querySelector(".preview-canvas"),a._i); });
  renderThemes(); // keep custom row swatches in sync
}

function buildCard(a, idx, levelNum) {
  const card=document.createElement("div"); card.dataset.idx=idx;
  const eff=a.effect||(a.game_over?"game_over":a.snake_mode?"snake":a.expand?"expand":a.gradient?"fade":"normal");
  card.className="alert-card"+(eff==="none"?" level-disabled":"")+(eff==="expand"?" popup-locked":"");
  const isSnake=eff==="snake", isGO=eff==="game_over";
  card.innerHTML=`
<div class="card-header">
  <span class="level-badge">Level ${levelNum}</span>
  <input class="f-mins-inline" type="number" value="${a.minutes_before}" min="0" max="120" step="1">
  <span class="mins-label">min before</span>
</div>
<div class="preview-wrap"><canvas class="preview-canvas"></canvas></div>
<div class="card-fields">
  <div class="field-row">
    <select class="f-effect">
      <option value="none"      ${eff==="none"     ?"selected":""}>None — no animation</option>
      <option value="normal"    ${eff==="normal"   ?"selected":""}>Normal — solid border, pulses</option>
      <option value="fade"      ${eff==="fade"     ?"selected":""}>Fade — gradient edge</option>
      <option value="expand"    ${eff==="expand"   ?"selected":""}>Expand — walls closing in</option>
      <option value="snake"     ${eff==="snake"    ?"selected":""}>Snake — crawling spiral</option>
      <option value="game_over" ${eff==="game_over"?"selected":""}>Game Over — full screen</option>
    </select>
    <div class="effect-note"${isGO?' style="display:block"':''}>Full screen takeover. Auto-dismisses after 2 min.</div>
  </div>
  <div class="two-col-row">
    <div class="field-group">
      <div class="field-label">Width</div>
      <div class="disc-btns">
        <button class="disc-btn f-width-btn" data-val="20">Thin</button>
        <button class="disc-btn f-width-btn" data-val="60">Med</button>
        <button class="disc-btn f-width-btn" data-val="140">Thick</button>
      </div>
    </div>
    <div class="field-group">
      <div class="field-label">Blink</div>
      <div class="disc-btns">
        <button class="disc-btn f-blink-btn" data-val="0">Off</button>
        <button class="disc-btn f-blink-btn" data-val="0.5">Slow</button>
        <button class="disc-btn f-blink-btn" data-val="4.0">Fast</button>
      </div>
    </div>
  </div>
  <div class="field-row">
    <div class="field-label">Popup</div>
    <div class="disc-btns">
      <button class="disc-btn f-popup-btn" data-val="none">None</button>
      <button class="disc-btn f-popup-btn" data-val="center">Center</button>
      <button class="disc-btn f-popup-btn" data-val="top">Top</button>
      <button class="disc-btn f-popup-btn" data-val="top-right">↗ Right</button>
    </div>
  </div>
  <div class="snake-fields"${isSnake?' style="display:block"':''}>
    <div class="field-row">
      <div class="field-label">Speed</div>
      <div class="disc-btns">
        <button class="disc-btn f-snake-btn" data-val="50">Slow</button>
        <button class="disc-btn f-snake-btn" data-val="300">Med</button>
        <button class="disc-btn f-snake-btn" data-val="600">Fast</button>
      </div>
    </div>
  </div>
  <button class="preview-btn" data-level="${levelNum}" style="margin-top:auto">▶ PREVIEW</button>
</div>`;
  wireCard(card,idx); return card;
}

function wireCard(card, idx) {
  card.querySelector(".f-mins-inline").addEventListener("input",e=>{state.alerts[idx].minutes_before=parseInt(e.target.value)||0;});
  const effectSel=card.querySelector(".f-effect"), snakeFields=card.querySelector(".snake-fields"), effectNote=card.querySelector(".effect-note");
  const pBtns=[...card.querySelectorAll(".f-popup-btn")];
  effectSel.addEventListener("change",()=>{
    const eff=effectSel.value; state.alerts[idx].effect=eff;
    snakeFields.style.display=eff==="snake"?"block":"none";
    effectNote.style.display=eff==="game_over"?"block":"none";
    card.classList.toggle("level-disabled", eff==="none");
    card.classList.toggle("popup-locked",   eff==="expand");
    if (eff==="expand") {
      // Force popup to center
      state.alerts[idx].popup_pos="center";
      pBtns.forEach(b=>b.classList.remove("active"));
      const cb=card.querySelector('.f-popup-btn[data-val="center"]');
      if(cb) cb.classList.add("active");
    }
    const cv=card.querySelector(".preview-canvas");
    if(cv){cv._coverage=0;cv._expandExtra=0;cv._snakeT=undefined;cv._expandT=undefined;}
  });
  const wBtns=[...card.querySelectorAll(".f-width-btn")];
  nearestDisc(wBtns,+(state.alerts[idx].width)||40).classList.add("active");
  wBtns.forEach(btn=>btn.addEventListener("click",()=>{wBtns.forEach(b=>b.classList.remove("active"));btn.classList.add("active");state.alerts[idx].width=parseInt(btn.dataset.val);}));
  const bBtns=[...card.querySelectorAll(".f-blink-btn")];
  nearestDisc(bBtns,+(state.alerts[idx].blink_hz)||0).classList.add("active");
  bBtns.forEach(btn=>btn.addEventListener("click",()=>{bBtns.forEach(b=>b.classList.remove("active"));btn.classList.add("active");state.alerts[idx].blink_hz=parseFloat(btn.dataset.val);}));
  const curPopup=state.alerts[idx].popup_pos||"center";
  (card.querySelector(`.f-popup-btn[data-val="${curPopup}"]`)||pBtns[1]).classList.add("active");
  pBtns.forEach(btn=>btn.addEventListener("click",()=>{pBtns.forEach(b=>b.classList.remove("active"));btn.classList.add("active");state.alerts[idx].popup_pos=btn.dataset.val;}));
  const sBtns=[...card.querySelectorAll(".f-snake-btn")];
  nearestDisc(sBtns,+(state.alerts[idx].snake_speed)||300).classList.add("active");
  sBtns.forEach(btn=>btn.addEventListener("click",()=>{sBtns.forEach(b=>b.classList.remove("active"));btn.classList.add("active");state.alerts[idx].snake_speed=parseInt(btn.dataset.val);}));
  card.querySelector(".preview-btn").addEventListener("click",async()=>{
    const n=+card.querySelector(".preview-btn").dataset.level;
    await saveConfig(true);
    try{const d=await(await fetch(`/api/preview/${n}`)).json();d.ok?toast(`▶ Level ${n} preview`):toast(d.error||"Preview failed",true);}
    catch(e){toast("Preview failed: "+e,true);}
  });
}

// ── Preview canvas ────────────────────────────────────────────────────────────
function initPreview(canvas, idx) {
  if (!canvas) return;
  const cw=canvas.parentElement.clientWidth-16;
  canvas.width=Math.max(60,cw); canvas.height=Math.round(canvas.width*REF_H/REF_W);
  canvas._coverage=0; canvas._snakeT=undefined; canvas._expandT=undefined; canvas._expandExtra=0;
  function frame(ts){drawPreview(canvas,state.alerts[idx],ts/1000);rafs[idx]=requestAnimationFrame(frame);}
  rafs[idx]=requestAnimationFrame(frame);
}

function hexToRgb(hex){const h=(hex||"#ff0000").replace("#","");return[parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];}
// If color is near-black, return red for popup accents (matches Python _popup_accent_rgb)
function accentRgb(r,g,b){return(r+g+b)<46?[255,0,0]:[r,g,b];}

function drawPreview(canvas, a, t) {
  if (!a) return;
  const ctx=canvas.getContext("2d"), W=canvas.width, H=canvas.height, scale=W/REF_W;
  const effect=a.effect||(a.game_over?"game_over":a.snake_mode?"snake":a.expand?"expand":a.gradient?"fade":"normal");
  const baseW=Math.max(1,Math.round((+a.width||40)*scale));
  const [r,g,b]=hexToRgb(a.color);
  ctx.clearRect(0,0,W,H); ctx.fillStyle="#060303"; ctx.fillRect(0,0,W,H);
  if (effect==="none") return;
  if (effect==="game_over") {
    ctx.fillStyle=`rgb(${r},${g},${b})`; ctx.fillRect(0,0,W,H);
    const popW=W*0.6,popH=H*0.2,popX=(W-W*0.6)/2,popY=(H-H*0.2)/2;
    const[ar,ag,ab]=accentRgb(r,g,b);
    ctx.fillStyle="rgba(5,2,2,0.92)"; ctx.fillRect(popX,popY,popW,popH);
    ctx.strokeStyle=`rgba(${ar},${ag},${ab},0.8)`; ctx.lineWidth=Math.max(1,W*0.004); ctx.strokeRect(popX,popY,popW,popH);
    return;
  }
  if (effect==="snake") {
    if (canvas._snakeT!==undefined) {
      const dt=Math.min(t-canvas._snakeT,0.1);
      const realP=2*(REF_W-(+a.width||40))+2*(REF_H-(+a.width||40));
      canvas._coverage=(canvas._coverage||0)+dt*(+(a.snake_speed)||300)/realP;
      const gap=2,rp=baseW+gap,maxL=Math.max(1,Math.floor((Math.min(W,H)/2-baseW/2)/rp));
      if (canvas._coverage>maxL) canvas._coverage=maxL;
    }
    canvas._snakeT=t; drawSnake(ctx,W,H,baseW,r,g,b,canvas._coverage||0); return;
  }
  let w=baseW;
  if (effect==="expand") {
    if (canvas._expandT!==undefined) {
      const dt=Math.min(t-canvas._expandT,0.1);
      canvas._expandExtra=(canvas._expandExtra||0)+dt*24.0;
      const maxExtra=Math.floor(W/2)-baseW;
      if (canvas._expandExtra>=maxExtra) canvas._expandExtra=maxExtra;
    }
    canvas._expandT=t; w=Math.min(Math.floor(W/2),baseW+(canvas._expandExtra||0));
  }
  let alpha=1.0;
  if ((+a.blink_hz||0)>0) alpha=0.4+0.6*Math.abs(Math.sin(Math.PI*a.blink_hz*t));
  if (effect==="expand") drawBorderGradient(ctx,W,H,w,r,g,b,alpha);
  else drawBorder(ctx,W,H,w,r,g,b,alpha);
}

function drawBorder(ctx,W,H,w,r,g,b,alpha){
  ctx.fillStyle=`rgba(${r},${g},${b},${alpha})`;
  ctx.fillRect(0,0,W,w);
  ctx.fillRect(0,H-w,W,w);
  ctx.fillRect(0,0,w,H);
  ctx.fillRect(W-w,0,w,H);
}

function drawBorderGradient(ctx,W,H,w,r,g,b,alpha){
  const solid=`rgba(${r},${g},${b},${alpha})`,clear=`rgba(${r},${g},${b},0)`;
  let g2;
  // Top: solid at y=0, clear at y=w
  g2=ctx.createLinearGradient(0,0,0,w); g2.addColorStop(0,solid); g2.addColorStop(1,clear);
  ctx.fillStyle=g2; ctx.fillRect(0,0,W,w);
  // Bottom: clear at y=H-w, solid at y=H
  g2=ctx.createLinearGradient(0,H-w,0,H); g2.addColorStop(0,clear); g2.addColorStop(1,solid);
  ctx.fillStyle=g2; ctx.fillRect(0,H-w,W,w);
  // Left: solid at x=0, clear at x=w
  g2=ctx.createLinearGradient(0,0,w,0); g2.addColorStop(0,solid); g2.addColorStop(1,clear);
  ctx.fillStyle=g2; ctx.fillRect(0,0,w,H);
  // Right: clear at x=W-w, solid at x=W
  g2=ctx.createLinearGradient(W-w,0,W,0); g2.addColorStop(0,clear); g2.addColorStop(1,solid);
  ctx.fillStyle=g2; ctx.fillRect(W-w,0,w,H);
}

function drawSnake(ctx,W,H,w,r,g,b,totalCoverage){
  // Continuous spiral: one stroke path, every ring uses shortened top + vertical step inward.
  // Canvas: y=0 at top; clockwise = right↓ bottom← left↑ top→ step↓.
  const gap=2,ringPitch=w+gap,loopCount=Math.floor(totalCoverage||0),frac=(totalCoverage||0)-loopCount;
  ctx.strokeStyle=`rgb(${r},${g},${b})`; ctx.lineWidth=w; ctx.lineJoin="miter"; ctx.lineCap="butt";
  ctx.beginPath(); let started=false;
  for(let ring=0;ring<=loopCount;ring++){
    const inset=ring*ringPitch,hw=w/2+inset;
    if(hw+w/2>=Math.min(W,H)/2) break;
    const isPartial=(ring===loopCount);
    const segs=[
      [W-hw, hw,          W-hw,          H-hw       ],  // right ↓
      [W-hw, H-hw,        hw,            H-hw       ],  // bottom ←
      [hw,   H-hw,        hw,            hw         ],  // left ↑
      [hw,   hw,          W-hw-ringPitch,hw         ],  // top → (shortened)
      [W-hw-ringPitch,hw, W-hw-ringPitch,hw+ringPitch],  // step ↓ inward
    ];
    const segLens=segs.map(([sx,sy,ex,ey])=>Math.hypot(ex-sx,ey-sy));
    const total=segLens.reduce((a,b)=>a+b,0);
    const targetLen=isPartial?frac*total:total;
    if(targetLen<=0) break;
    let rem=targetLen;
    for(let i=0;i<segs.length&&rem>0;i++){
      const[sx,sy,ex,ey]=segs[i];
      if(!started){ctx.moveTo(sx,sy);started=true;}
      if(rem>=segLens[i]){ctx.lineTo(ex,ey);rem-=segLens[i];}
      else{const f=rem/segLens[i];ctx.lineTo(sx+f*(ex-sx),sy+f*(ey-sy));rem=0;}
    }
    if(isPartial) break;
  }
  if(started) ctx.stroke();
}

// ── Save ──────────────────────────────────────────────────────────────────────
async function saveConfig(silent=false){
  state.calendars=(document.getElementById("cals-input").value.trim()||"").split("\n").map(s=>s.trim()).filter(Boolean);
  try{
    const d=await(await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({calendars:state.calendars,alerts:state.alerts,popup_font:state.popup_font,popup_pos:state.popup_pos,default_name:state.default_name})})).json();
    if(!silent) d.ok?toast("✓ Saved — restart Hardstop to apply"):toast("Error: "+d.error,true);
    return d.ok;
  }catch(e){if(!silent)toast("Save failed: "+e,true);return false;}
}
document.getElementById("save-btn").addEventListener("click",()=>saveConfig(false));

// ── Toast ─────────────────────────────────────────────────────────────────────
let _tt=null;
function toast(msg,err=false){
  const el=document.getElementById("toast"); el.textContent=msg; el.className="show"+(err?" err":"");
  if(_tt)clearTimeout(_tt); _tt=setTimeout(()=>{el.className="";},3200);
}

boot();
</script>
</body>
</html>"""





# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    import signal
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        print("AppKit not available. Install: pip install pyobjc-framework-Cocoa")
        sys.exit(1)

    APP_DIR.mkdir(parents=True, exist_ok=True)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # Allow Ctrl-C to quit cleanly
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))

    delegate = _AppDelegate.new()
    app.setDelegate_(delegate)
    print("Hardstop running in menu bar. Ctrl+C to quit.")
    app.run()


if __name__ == "__main__":
    main()
