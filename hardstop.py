#!/usr/bin/env python3
"""
hardstop — Google Calendar & deadline overlay for ultrawide monitors.

Runs as a macOS menu bar app. Polls Google Calendar and lets you set manual
"hardstop" deadlines. Both trigger escalating screen-border animations so you
can't miss a meeting even on a giant ultrawide.

Setup:
  pip install -e .
  # Place client_secret.json at ~/.hardstop/client_secret.json
  hardstop   # opens browser for Google auth on first run
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

APP_DIR            = Path.home() / ".hardstop"
CONFIG_PATH        = APP_DIR / "config.yaml"
HARDSTOP_PATH      = APP_DIR / "hardstop.json"
TOKEN_PATH         = APP_DIR / "token.json"
CLIENT_SECRET_PATH = APP_DIR / "client_secret.json"
LAUNCH_AGENT_LABEL = "com.hardstop"
LAUNCH_AGENT_PATH  = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
CALENDAR_SCOPES    = ["https://www.googleapis.com/auth/calendar.readonly"]

# Float above all apps and full-screen spaces
_OVERLAY_LEVEL = 1001

DEFAULT_CONFIG = {
    "calendars": [],
    "popup_font": "modern",   # "modern" | "retro"
    "popup_pos":  "center",   # "center" | "top" | "snake"
    "alerts": [
        {
            "minutes_before": 5,
            "color": "#FF8C00",
            "width": 40,
            "blink_hz": 0.5,
            "expand": False,
            "gradient": True,
            "snake_mode": False,
            "snake_speed": 80,
            "snake_start": 0.0,
            "game_over": False,
        },
        {
            "minutes_before": 2,
            "color": "#FF4500",
            "width": 80,
            "blink_hz": 2.0,
            "expand": True,
            "gradient": True,
            "snake_mode": False,
            "snake_speed": 160,
            "snake_start": 0.25,
            "game_over": False,
        },
        {
            "minutes_before": 0,
            "color": "#FF0000",
            "width": 120,
            "blink_hz": 4.0,
            "expand": False,
            "gradient": False,
            "snake_mode": False,
            "snake_speed": 320,
            "snake_start": 0.5,
            "game_over": False,
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
        return {**DEFAULT_CONFIG, **data}
    except Exception as e:
        print(f"Config load error: {e} — using defaults.")
        return DEFAULT_CONFIG.copy()


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


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

    if not CLIENT_SECRET_PATH.exists():
        print(
            f"client_secret.json not found at {CLIENT_SECRET_PATH}\n"
            "Download OAuth credentials from console.cloud.google.com and place it there."
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
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_PATH), CALENDAR_SCOPES
            )
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

    Accepted formats:
      "4:55pm", "4:55 PM", "16:55"  — time of day (today or tomorrow if past)
      "30m", "30min", "30 min"       — 30 minutes from now
      "1h", "1h30m", "1 hour 30 min" — duration from now
      "45"                            — bare integer = minutes from now
    """
    text = text.strip().lower()
    # Normalise unit aliases before collapsing spaces
    text = re.sub(r"\bhours?\b", "h", text)
    text = re.sub(r"\bmin(?:utes?)?\b", "m", text)
    text = re.sub(r"\s+", "", text)   # drop all remaining spaces
    now = datetime.now().astimezone()

    # Hours + minutes: "1h30m", "30m", "90m"
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

    # Time of day
    for fmt in ("%I:%M%p", "%H:%M", "%I%p"):
        try:
            t = datetime.strptime(text, fmt)
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
        return self

    @objc.python_method
    def configure(self, alert_cfg: dict) -> None:
        self._cfg = alert_cfg
        self._extra_width = 0.0
        self._snake_coverage = alert_cfg.get("snake_start", 0.0)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_extra_width(self, w: float) -> None:
        self._extra_width = w
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_snake_coverage(self, coverage: float) -> None:
        self._snake_coverage = min(1.0, max(0.0, coverage))
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if not self._cfg:
            return

        from AppKit import NSColor
        from Foundation import NSMakeRect

        cfg = self._cfg
        r, g, b = _hex_to_rgb(cfg.get("color", "#FF0000"))
        w = cfg.get("width", 40) + self._extra_width
        fw = self.frame().size.width
        fh = self.frame().size.height
        color = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)

        if cfg.get("snake_mode", False):
            self._draw_snake(fw, fh, w, color)
        else:
            self._draw_border(fw, fh, w, color, cfg)

    @objc.python_method
    def _draw_border(self, fw, fh, w, color, cfg):
        from AppKit import NSBezierPath, NSGradient, NSColor
        from Foundation import NSMakeRect

        use_gradient = cfg.get("gradient", False) or cfg.get("expand", False)
        r, g, b = _hex_to_rgb(cfg.get("color", "#FF0000"))
        clear = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.0)

        # Angle: 0=left→right, 90=bottom→top, 180=right→left, 270=top→bottom
        # [clear, color]: inner edge = transparent, outer edge = solid
        strips = [
            (NSMakeRect(0,      fh - w, fw, w), 90),   # top
            (NSMakeRect(0,      0,      fw, w), 270),  # bottom
            (NSMakeRect(0,      0,      w,  fh), 180), # left
            (NSMakeRect(fw - w, 0,      w,  fh), 0),   # right
        ]

        for strip, angle in strips:
            if use_gradient:
                NSGradient.alloc().initWithColors_([clear, color]).drawInRect_angle_(strip, angle)
            else:
                color.set()
                NSBezierPath.fillRect_(strip)

    @objc.python_method
    def _draw_snake(self, fw, fh, w, color):
        """
        Clockwise snake crawling around the border center-line.
        Hard edges (NSButtLineCapStyle). Coverage [0,1] drives how far it extends.
        """
        from AppKit import NSBezierPath, NSButtLineCapStyle, NSMiterLineJoinStyle

        hw = w / 2  # center-line offset from screen edge

        # Clockwise from top-left: top → right → bottom → left
        segments = [
            (hw,      fh - hw, fw - hw, fh - hw),  # top
            (fw - hw, fh - hw, fw - hw, hw),        # right
            (fw - hw, hw,      hw,      hw),         # bottom
            (hw,      hw,      hw,      fh - hw),   # left
        ]

        seg_lengths = [
            math.hypot(ex - sx, ey - sy)
            for sx, sy, ex, ey in segments
        ]
        perimeter = sum(seg_lengths)
        target_len = self._snake_coverage * perimeter
        if target_len <= 0:
            return

        path = NSBezierPath.bezierPath()
        path.setLineWidth_(w)
        path.setLineCapStyle_(NSButtLineCapStyle)
        path.setLineJoinStyle_(NSMiterLineJoinStyle)

        remaining = target_len
        first = True
        for (sx, sy, ex, ey), seg_len in zip(segments, seg_lengths):
            if remaining <= 0:
                break
            if first:
                path.moveToPoint_((sx, sy))
                first = False
            if remaining >= seg_len:
                path.lineToPoint_((ex, ey))
                remaining -= seg_len
            else:
                t = remaining / seg_len
                path.lineToPoint_((sx + t * (ex - sx), sy + t * (ey - sy)))
                remaining = 0

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
        self._popup_font = "modern"
        self._game_over  = False
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
        self._game_over  = game_over
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
            return f"in {m}:{s:02d}"
        return "NOW"

    @objc.python_method
    def _accent_color(self):
        from AppKit import NSColor
        r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF8C00")) if self._cfg else (1.0, 0.5, 0.0)
        return NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)

    def drawRect_(self, rect):
        if self._game_over:
            self._draw_game_over()
        elif self._popup_font == "retro":
            self._draw_retro()
        else:
            self._draw_modern()

    @objc.python_method
    def _draw_modern(self):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
            NSCenterTextAlignment, NSLeftTextAlignment,
        )
        from Foundation import NSMakeRect

        fw, fh = self.frame().size.width, self.frame().size.height
        pad, radius = 20.0, 20.0

        # Background pill
        NSColor.colorWithRed_green_blue_alpha_(0.04, 0.04, 0.06, 0.95).set()
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, fw, fh), radius, radius)
        pill.fill()
        self._accent_color().colorWithAlphaComponent_(0.85).set()
        pill.setLineWidth_(2.0)
        pill.stroke()

        btn_w, btn_h = 92.0, 36.0
        btn_gap = 10.0
        btn_y = (fh - btn_h) / 2
        dismiss_x = fw - pad - btn_w
        snooze_x  = dismiss_x - btn_gap - btn_w
        text_w    = snooze_x - pad - 12.0
        self._dismiss_rect = (dismiss_x, btn_y, btn_w, btn_h)
        self._snooze_rect  = (snooze_x,  btn_y, btn_w, btn_h)

        cps = NSMutableParagraphStyle.new(); cps.setAlignment_(NSCenterTextAlignment)
        lps = NSMutableParagraphStyle.new(); lps.setAlignment_(NSLeftTextAlignment)

        NSAttributedString.alloc().initWithString_attributes_(self._label, {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(24),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
            NSParagraphStyleAttributeName: lps,
        }).drawInRect_(NSMakeRect(pad, fh / 2 + 2, text_w, 30))

        NSAttributedString.alloc().initWithString_attributes_(self._countdown(), {
            NSFontAttributeName: NSFont.systemFontOfSize_(15),
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.65, 1.0),
            NSParagraphStyleAttributeName: lps,
        }).drawInRect_(NSMakeRect(pad, fh / 2 - 20, text_w, 20))

        self._draw_btn("Snooze",  self._snooze_rect,  primary=False, cps=cps, font_size=13)
        self._draw_btn("Dismiss", self._dismiss_rect, primary=True,  cps=cps, font_size=13)

    @objc.python_method
    def _draw_retro(self):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
            NSCenterTextAlignment, NSLeftTextAlignment,
        )
        from Foundation import NSMakeRect

        fw, fh = self.frame().size.width, self.frame().size.height
        pad = 20.0

        # Dark terminal background — sharp corners
        NSColor.colorWithRed_green_blue_alpha_(0.0, 0.05, 0.02, 0.97).set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, fw, fh))
        # Green border
        NSColor.colorWithRed_green_blue_alpha_(0.0, 0.85, 0.3, 1.0).set()
        border = NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, fw, fh))
        border.setLineWidth_(2.0)
        border.stroke()

        # Retro font: prefer Press Start 2P (if installed), fall back to Monaco
        def retro_font(size):
            f = NSFont.fontWithName_size_("Press Start 2P", size)
            if f is None:
                f = NSFont.fontWithName_size_("Monaco", size)
            return f or NSFont.boldSystemFontOfSize_(size)

        btn_w, btn_h = 92.0, 36.0
        btn_gap = 10.0
        btn_y = (fh - btn_h) / 2
        dismiss_x = fw - pad - btn_w
        snooze_x  = dismiss_x - btn_gap - btn_w
        text_w    = snooze_x - pad - 12.0
        self._dismiss_rect = (dismiss_x, btn_y, btn_w, btn_h)
        self._snooze_rect  = (snooze_x,  btn_y, btn_w, btn_h)

        cps = NSMutableParagraphStyle.new(); cps.setAlignment_(NSCenterTextAlignment)
        lps = NSMutableParagraphStyle.new(); lps.setAlignment_(NSLeftTextAlignment)
        green = NSColor.colorWithRed_green_blue_alpha_(0.0, 0.95, 0.35, 1.0)
        dim_green = NSColor.colorWithRed_green_blue_alpha_(0.0, 0.60, 0.25, 1.0)

        NSAttributedString.alloc().initWithString_attributes_(
                self._label.upper(), {
            NSFontAttributeName: retro_font(18),
            NSForegroundColorAttributeName: green,
            NSParagraphStyleAttributeName: lps,
        }).drawInRect_(NSMakeRect(pad, fh / 2 + 2, text_w, 28))

        NSAttributedString.alloc().initWithString_attributes_(
                f"> {self._countdown()}", {
            NSFontAttributeName: retro_font(11),
            NSForegroundColorAttributeName: dim_green,
            NSParagraphStyleAttributeName: lps,
        }).drawInRect_(NSMakeRect(pad, fh / 2 - 20, text_w, 20))

        self._draw_btn("SNOOZE",  self._snooze_rect,  primary=False, cps=cps,
                       font_size=10, font_name="Monaco")
        self._draw_btn("DISMISS", self._dismiss_rect, primary=True,  cps=cps,
                       font_size=10, font_name="Monaco")

    @objc.python_method
    def _draw_game_over(self):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
            NSCenterTextAlignment,
        )
        from Foundation import NSMakeRect

        fw, fh = self.frame().size.width, self.frame().size.height
        pad = 24.0

        # Full dark background
        NSColor.colorWithRed_green_blue_alpha_(0.03, 0.0, 0.0, 0.97).set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, fw, fh))

        # Thick red border
        r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF0000")) if self._cfg else (1, 0, 0)
        NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0).set()
        border = NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, fw, fh))
        border.setLineWidth_(4.0)
        border.stroke()

        cps = NSMutableParagraphStyle.new(); cps.setAlignment_(NSCenterTextAlignment)

        # "GAME OVER" — huge
        NSAttributedString.alloc().initWithString_attributes_("GAME OVER", {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(72),
            NSForegroundColorAttributeName: NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0),
            NSParagraphStyleAttributeName: cps,
        }).drawInRect_(NSMakeRect(0, fh * 0.52, fw, 88))

        # Event name
        NSAttributedString.alloc().initWithString_attributes_(self._label, {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(26),
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.85, 1.0),
            NSParagraphStyleAttributeName: cps,
        }).drawInRect_(NSMakeRect(pad, fh * 0.34, fw - 2*pad, 34))

        # Countdown
        NSAttributedString.alloc().initWithString_attributes_(self._countdown(), {
            NSFontAttributeName: NSFont.systemFontOfSize_(18),
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.55, 1.0),
            NSParagraphStyleAttributeName: cps,
        }).drawInRect_(NSMakeRect(0, fh * 0.23, fw, 24))

        # Buttons side by side centered
        btn_w, btn_h = 120.0, 40.0
        btn_gap = 16.0
        total_w = btn_w * 2 + btn_gap
        bx = (fw - total_w) / 2
        by = fh * 0.08
        self._snooze_rect  = (bx,              by, btn_w, btn_h)
        self._dismiss_rect = (bx + btn_w + btn_gap, by, btn_w, btn_h)
        self._draw_btn("Snooze",  self._snooze_rect,  primary=False, cps=cps, font_size=14)
        self._draw_btn("Dismiss", self._dismiss_rect, primary=True,  cps=cps, font_size=14)

    @objc.python_method
    def _draw_btn(self, title, btn_rect, primary, cps, font_size=13, font_name=None):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSForegroundColorAttributeName, NSFontAttributeName,
            NSParagraphStyleAttributeName,
        )
        from Foundation import NSMakeRect

        bx, by, bw, bh = btn_rect
        if primary:
            r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF8C00")) if self._cfg else (1, 0, 0)
            NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.35).set()
        else:
            NSColor.colorWithWhite_alpha_(1.0, 0.12).set()

        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, bw, bh), 6, 6).fill()

        if font_name:
            font = NSFont.fontWithName_size_(font_name, font_size) or NSFont.systemFontOfSize_(font_size)
        else:
            font = NSFont.systemFontOfSize_(font_size)

        NSAttributedString.alloc().initWithString_attributes_(title, {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.92, 1.0),
            NSParagraphStyleAttributeName: cps,
        }).drawInRect_(NSMakeRect(bx + 2, by + (bh - font_size) / 2 - 1, bw - 4, font_size + 4))

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
            self._dismiss_cb()
        elif _hit(self._snooze_rect) and self._snooze_cb:
            self._snooze_cb()

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
        self._popup_pos   = "center"  # "center" | "top" | "snake"

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
        self._start_time  = time.time()

        from AppKit import NSScreen
        frame = NSScreen.mainScreen().frame()
        self._make_border(frame, alert_cfg)
        self._make_banner(frame, label, start_dt, alert_cfg)

        from Foundation import NSTimer
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30.0, tick_target, "overlayTick:", None, True
        )

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
        popup_font  = global_cfg.get("popup_font", "modern")
        popup_pos   = global_cfg.get("popup_pos",  "center")
        game_over   = cfg.get("game_over", False)
        self._popup_pos = popup_pos

        sw, sh = screen_frame.size.width, screen_frame.size.height

        if game_over:
            bw = min(sw * 0.72, 860.0)
            bh = 240.0
        else:
            bw = min(sw * 0.52, 680.0)
            bh = 120.0

        if popup_pos == "top":
            bx = (sw - bw) / 2
            by = sh - bh - 60
        else:  # center or snake (snake starts centered, moves in tick)
            bx = (sw - bw) / 2
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

        cfg = self._current_cfg
        t   = time.time()
        blink_hz = cfg.get("blink_hz", 0)
        # Snake mode is always fully opaque — blink doesn't apply
        is_snake = cfg.get("snake_mode", False)
        alpha = (
            0.4 + 0.6 * abs(math.sin(math.pi * blink_hz * t))
            if blink_hz > 0 and not is_snake else 1.0
        )
        self._border_win.setAlphaValue_(alpha)

        if cfg.get("snake_mode", False):
            fw = self._border_view.frame().size.width
            fh = self._border_view.frame().size.height
            w  = cfg.get("width", 40)
            perimeter = 2 * (fw - w) + 2 * (fh - w)
            if perimeter > 0:
                elapsed    = t - self._start_time
                start_frac = cfg.get("snake_start", 0.0)
                speed_frac = cfg.get("snake_speed", 80) / perimeter
                self._border_view.set_snake_coverage(start_frac + elapsed * speed_frac)

            # Snake head: move banner to follow the snake tip
            if (self._popup_pos == "snake" and self._banner_win and
                    self._border_view and self._border_win):
                hx, hy = self._border_view.snake_head_position()
                sf  = self._border_win.frame()
                bsz = self._banner_win.frame().size
                bw, bh = bsz.width, bsz.height
                ox, oy = sf.origin.x, sf.origin.y
                sw, sh = sf.size.width, sf.size.height
                nx = max(ox, min(ox + hx - bw / 2, ox + sw - bw))
                ny = max(oy, min(oy + hy - bh / 2, oy + sh - bh))
                self._banner_win.setFrameOrigin_((nx, ny))

        elif cfg.get("expand"):
            elapsed = t - self._start_time
            self._border_view.set_extra_width(elapsed * 4.0)  # 4 px/sec

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

        for title, action in [
            ("Set Hardstop…", "setHardstop:"),
            ("Edit Config…",  "editConfig:"),
        ]:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            mi.setTarget_(self)
            menu.addItem_(mi)

        menu.addItem_(NSMenuItem.separatorItem())

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
        alert.setMessageText_("Set Hardstop")
        alert.setInformativeText_("Name your hardstop and enter a time or duration.")
        alert.addButtonWithTitle_("Set")
        alert.addButtonWithTitle_("Cancel")

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 58))

        name_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 32, 300, 24))
        name_field.setPlaceholderString_("Name  (e.g. Catch Bus, End of Day…)")

        time_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        time_field.setPlaceholderString_("Time: 4:55pm  or  Duration: 30m, 1h30m")

        container.addSubview_(name_field)
        container.addSubview_(time_field)
        alert.setAccessoryView_(container)
        alert.window().setInitialFirstResponder_(name_field)

        if alert.runModal() == 1000:  # NSAlertFirstButtonReturn
            name = name_field.stringValue().strip() or "Hardstop"
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
        return jsonify({"authorized": authorized})

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
  --text:#ddd0cf;--muted:#887070;--dim:#aa8888;--success:#44cc66;--cut:6px;
}
.octo{clip-path:polygon(var(--cut) 0,calc(100% - var(--cut)) 0,100% var(--cut),100% calc(100% - var(--cut)),calc(100% - var(--cut)) 100%,var(--cut) 100%,0 calc(100% - var(--cut)),0 var(--cut));border-radius:0 !important}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:13px;min-height:100vh}
header{position:sticky;top:0;z-index:100;background:#0e0808;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;padding:12px 20px}
.logo-svg{flex-shrink:0}
header h1{flex:1;font-size:14px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--text)}
header h1 span{color:var(--accent-hi)}
#save-btn{background:var(--accent);color:#fff;border:none;padding:7px 22px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.05em;transition:background .15s}
#save-btn:hover{background:var(--accent-hi)}
#save-btn:active{background:#991800}
main{max-width:960px;margin:0 auto;padding:20px 16px 60px}
.section-label{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}

/* alerts grid */
.alerts-grid{display:flex;flex-wrap:nowrap;gap:10px;overflow-x:auto;align-items:stretch;padding-bottom:4px;margin-bottom:18px;scrollbar-width:thin;scrollbar-color:var(--accent-dim) transparent}
.alerts-grid::-webkit-scrollbar{height:4px}
.alerts-grid::-webkit-scrollbar-thumb{background:var(--accent-dim)}
.alert-card{flex:1 1 0;min-width:200px;background:var(--card);border:1px solid var(--border);overflow:hidden}
.alerts-grid.carousel .alert-card{flex:0 0 270px}
.alert-card:hover{border-color:#3d2020}
.add-card{flex:0 0 48px;background:none;border:1px dashed #441111;color:#663333;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:200;min-height:160px;transition:all .15s}
.add-card:hover{border-color:var(--accent);color:var(--accent-hi);background:#110505}

/* card parts */
.card-header{display:flex;align-items:center;gap:8px;padding:9px 10px;background:#161010;border-bottom:1px solid var(--border)}
.level-badge{background:var(--accent-dim);color:var(--accent-hi);font-size:9px;font-weight:800;letter-spacing:.1em;padding:2px 7px;text-transform:uppercase;flex-shrink:0}
.mins-badge{flex:1;font-size:11px;color:var(--dim)}
.remove-btn{background:none;border:1px solid #441111;color:#884444;width:20px;height:20px;cursor:pointer;font-size:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .15s}
.remove-btn:hover{background:#2a0808;border-color:var(--accent);color:var(--accent-hi)}
.preview-wrap{padding:8px 8px 0;background:#090505}
.preview-canvas{display:block;width:100%;background:#060303}
.mode-tabs{display:flex;gap:0;padding:8px 8px 0}
.mode-tab{flex:1;background:var(--accent-dim);border:1px solid var(--border);color:var(--muted);padding:5px 0;font-size:11px;font-weight:700;letter-spacing:.05em;cursor:pointer;text-align:center;transition:all .15s;border-radius:0}
.mode-tab:first-child{border-right:none}
.mode-tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.mode-tab:hover:not(.active){background:#2a0e0e;color:var(--dim)}
.card-fields{padding:10px 10px 12px;display:flex;flex-direction:column;gap:10px}
.field-row{display:flex;flex-direction:column;gap:4px}
.field-label{font-size:10px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
input[type="number"]{background:#0a0505;border:1px solid var(--border);border-radius:0;color:var(--text);padding:5px 8px;font-size:13px;width:72px;outline:none}
input[type="number"]:focus{border-color:var(--accent)}
.color-row{display:flex;align-items:center;gap:7px}
input[type="color"]{width:32px;height:26px;border:1px solid var(--border);border-radius:0;background:none;cursor:pointer;padding:2px;flex-shrink:0}
.color-hex{background:#0a0505;border:1px solid var(--border);border-radius:0;color:var(--text);padding:4px 8px;font-size:12px;font-family:"SF Mono",Menlo,monospace;width:80px;outline:none}
.color-hex:focus{border-color:var(--accent)}
.slider-row{display:flex;align-items:center;gap:8px}
input[type="range"]{-webkit-appearance:none;flex:1;height:3px;background:#2a1515;border-radius:0;outline:none;cursor:pointer}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;background:var(--accent-hi);cursor:pointer;border:2px solid #0c0c0c;clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%)}
.sval{font-size:11px;font-family:"SF Mono",Menlo,monospace;color:var(--dim);min-width:52px;text-align:right}
.toggle-row{display:flex;align-items:center;gap:8px}
.toggle-label{flex:1;font-size:11px;color:var(--dim)}
.toggle{position:relative;width:32px;height:18px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.ttrack{position:absolute;inset:0;background:#331515;border:1px solid #441111;cursor:pointer;transition:all .2s;clip-path:polygon(3px 0,calc(100% - 3px) 0,100% 3px,100% calc(100% - 3px),calc(100% - 3px) 100%,3px 100%,0 calc(100% - 3px),0 3px)}
.toggle input:checked + .ttrack{background:#882200;border-color:#aa3300}
.ttrack::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;background:#776060;transition:all .2s;clip-path:polygon(2px 0,calc(100% - 2px) 0,100% 2px,100% calc(100% - 2px),calc(100% - 2px) 100%,2px 100%,0 calc(100% - 2px),0 2px)}
.toggle input:checked + .ttrack::after{left:calc(100% - 14px);background:var(--accent-hi)}

/* segmented controls */
.seg-control{display:flex;gap:0;margin-top:4px}
.seg-btn{flex:1;background:var(--accent-dim);border:1px solid var(--border);color:var(--muted);padding:5px 0;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:all .15s;border-radius:0}
.seg-btn:not(:first-child){border-left:none}
.seg-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.seg-btn:hover:not(.active){background:#2a0e0e;color:var(--dim)}

/* width max */
.f-width-max{background:none;border:1px solid #441111;color:#663333;padding:3px 7px;font-size:10px;font-weight:700;letter-spacing:.06em;cursor:pointer;flex-shrink:0;transition:all .15s}
.f-width-max:hover,.f-width-max.active{border-color:var(--accent);color:var(--accent-hi);background:#1a0505}

/* preview & action buttons */
.preview-btn{width:100%;background:none;border:1px solid #441111;color:#884444;padding:7px 0;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:4px}
.preview-btn:hover{border-color:var(--accent);color:var(--accent-hi);background:#110505}

/* popup & calendar settings panels */
.panel-wrap{background:var(--card);border:1px solid var(--border);margin-bottom:18px;overflow:hidden}
.panel-wrap summary{list-style:none;padding:10px 14px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);user-select:none;display:flex;align-items:center;gap:8px}
.panel-wrap summary::before{content:"▸";font-size:10px;color:var(--accent);transition:transform .2s}
.panel-wrap[open] summary::before{transform:rotate(90deg)}
.panel-wrap summary::-webkit-details-marker{display:none}
.panel-inner{padding:10px 14px 14px;display:flex;flex-direction:column;gap:12px}
.panel-row{display:flex;align-items:center;gap:12px}
.panel-row-label{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);min-width:80px}

/* calendar specifics */
.cal-auth-row{display:flex;align-items:center;gap:10px;padding-bottom:12px;border-bottom:1px solid var(--border)}
.cal-auth-status{flex:1;font-size:12px;color:var(--muted)}
.cal-auth-status.ok{color:var(--success)}
.cal-auth-btn{background:var(--accent-dim);color:var(--dim);border:1px solid var(--border);padding:5px 12px;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:.05em;transition:all .15s}
.cal-auth-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
#cals-input{width:100%;background:#090404;border:1px solid var(--border);color:var(--text);padding:7px 10px;font-size:12px;font-family:"SF Mono",Menlo,monospace;outline:none;resize:vertical;min-height:44px}
#cals-input:focus{border-color:var(--accent)}
.cals-hint{font-size:11px;color:var(--muted);margin-top:4px}

/* toast */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(16px);background:#1a2a1a;border:1px solid #335533;color:var(--success);padding:9px 18px;font-size:13px;font-weight:600;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;clip-path:polygon(5px 0,calc(100% - 5px) 0,100% 5px,100% calc(100% - 5px),calc(100% - 5px) 100%,5px 100%,0 calc(100% - 5px),0 5px)}
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
  <button id="save-btn" class="octo">Save</button>
</header>

<main>
  <div class="section-label">Alert Levels — most to least urgent, left to right</div>
  <div id="alerts-grid" class="alerts-grid"></div>

  <details class="panel-wrap octo" id="popup-panel">
    <summary>Popup Settings</summary>
    <div class="panel-inner">
      <div class="panel-row">
        <div class="panel-row-label">Font Style</div>
        <div class="seg-control" id="font-seg" style="flex:1">
          <button class="seg-btn" data-font="modern">Modern</button>
          <button class="seg-btn" data-font="retro">Retro</button>
        </div>
      </div>
      <div class="panel-row">
        <div class="panel-row-label">Position</div>
        <div class="seg-control" id="pos-seg" style="flex:1">
          <button class="seg-btn" data-pos="center">Center</button>
          <button class="seg-btn" data-pos="top">Top</button>
          <button class="seg-btn" data-pos="snake">Snake Head</button>
        </div>
      </div>
    </div>
  </details>

  <details class="panel-wrap octo">
    <summary>Google Calendar Settings</summary>
    <div class="panel-inner">
      <div class="cal-auth-row">
        <span class="cal-auth-status" id="cal-auth-status">Checking…</span>
        <button class="cal-auth-btn octo" id="cal-auth-btn">Authorize</button>
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
  minutes_before:5, color:"#FF8C00",
  width:40, blink_hz:0.5,
  expand:false, gradient:true,
  snake_mode:false, snake_speed:80, snake_start:0.0,
  game_over:false,
};

let state = { calendars:[], alerts:[], popup_font:"modern", popup_pos:"center" };
let rafs = {};

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    state.calendars  = cfg.calendars  || [];
    state.alerts     = cfg.alerts     || [];
    state.popup_font = cfg.popup_font || "modern";
    state.popup_pos  = cfg.popup_pos  || "center";
    document.getElementById("cals-input").value = state.calendars.join("\n");
    initPopupControls();
    renderAlerts();
  } catch(e) { toast("Failed to load config: " + e, true); }
  checkAuthStatus();
}

// ── Popup settings ─────────────────────────────────────────────────────────────
function initPopupControls() {
  document.querySelectorAll("#font-seg .seg-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.font === state.popup_font);
    btn.addEventListener("click", () => {
      document.querySelectorAll("#font-seg .seg-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.popup_font = btn.dataset.font;
    });
  });
  document.querySelectorAll("#pos-seg .seg-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.pos === state.popup_pos);
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pos-seg .seg-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.popup_pos = btn.dataset.pos;
    });
  });
}

// ── Calendar auth ──────────────────────────────────────────────────────────────
async function checkAuthStatus() {
  try {
    const r = await fetch("/api/auth_status");
    const d = await r.json();
    const el = document.getElementById("cal-auth-status");
    const btn = document.getElementById("cal-auth-btn");
    if (d.authorized) {
      el.textContent = "✓ Google Calendar authorized";
      el.className = "cal-auth-status ok";
      btn.textContent = "Re-authorize";
    } else {
      el.textContent = "Not authorized — calendar polling disabled";
      el.className = "cal-auth-status";
      btn.textContent = "Authorize Google Calendar";
    }
  } catch(e) {}
}
document.getElementById("cal-auth-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/authorize", { method:"POST" });
    toast("Browser will open for Google Calendar authorization");
    setTimeout(checkAuthStatus, 8000);
  } catch(e) { toast("Authorization failed: " + e, true); }
});

// ── Render alerts ─────────────────────────────────────────────────────────────
function renderAlerts() {
  Object.values(rafs).forEach(cancelAnimationFrame);
  rafs = {};
  const grid = document.getElementById("alerts-grid");
  grid.innerHTML = "";
  grid.classList.toggle("carousel", state.alerts.length > 3);

  const order = [...state.alerts]
    .map((a,i) => ({...a, _i:i}))
    .sort((a,b) => b.minutes_before - a.minutes_before);

  order.forEach((a, pos) => {
    const card = buildCard(a, a._i, pos+1);
    grid.appendChild(card);
    initPreview(card.querySelector(".preview-canvas"), a._i);
  });

  const addCard = document.createElement("button");
  addCard.className = "add-card";
  addCard.title = "Add alert level";
  addCard.textContent = "+";
  addCard.addEventListener("click", () => {
    state.alerts.push({...DEFAULT_ALERT, minutes_before:10});
    renderAlerts();
    grid.scrollTo({left:grid.scrollWidth, behavior:"smooth"});
  });
  grid.appendChild(addCard);
}

function buildCard(a, idx, levelNum) {
  const card = document.createElement("div");
  card.className = "alert-card";
  card.dataset.idx = idx;
  const minsLabel = a.minutes_before===0 ? "At meeting start"
                  : a.minutes_before===1 ? "1 minute before"
                  : `${a.minutes_before} minutes before`;
  const isSnake = !!a.snake_mode;
  // effect: none | fade | expand
  const eff = a.expand ? "expand" : a.gradient ? "fade" : "none";

  card.innerHTML = `
<div class="card-header">
  <span class="level-badge octo">LVL ${levelNum}</span>
  <span class="mins-badge f-mins-label">${minsLabel}</span>
  <button class="remove-btn octo" title="Remove">✕</button>
</div>
<div class="preview-wrap">
  <canvas class="preview-canvas" height="80"></canvas>
</div>
<div class="mode-tabs">
  <button class="mode-tab octo ${isSnake?'':'active'}" data-mode="perimeter">Perimeter</button>
  <button class="mode-tab octo ${isSnake?'active':''}" data-mode="snake">Snake</button>
</div>
<div class="card-fields">
  <div class="field-row">
    <div class="field-label">Minutes before</div>
    <input class="f-mins" type="number" value="${a.minutes_before}" min="0" max="120" step="1">
  </div>
  <div class="field-row">
    <div class="field-label">Color</div>
    <div class="color-row">
      <input class="f-color" type="color" value="${a.color}">
      <input class="f-hex color-hex" type="text" value="${a.color}" maxlength="7">
    </div>
  </div>
  <div class="field-row">
    <div class="field-label">Border width</div>
    <div class="slider-row">
      <input class="f-width" type="range" min="10" max="400" value="${Math.min(+a.width||40,400)}">
      <span class="sval f-width-v">${+a.width>=1000?'Max':(+a.width||40)+'px'}</span>
      <button class="f-width-max octo${+a.width>=1000?' active':''}" title="Fill screen">Max</button>
    </div>
  </div>
  <!-- PERIMETER fields -->
  <div class="perimeter-fields" style="${isSnake?'display:none':''}">
    <div class="field-row">
      <div class="field-label">Blink rate</div>
      <div class="slider-row">
        <input class="f-blink" type="range" min="0" max="10" step="0.1" value="${a.blink_hz}">
        <span class="sval f-blink-v">${(+a.blink_hz).toFixed(1)} Hz</span>
      </div>
    </div>
    <div class="field-row">
      <div class="field-label">Effect</div>
      <div class="seg-control">
        <button class="seg-btn${eff==='none'?' active':''}" data-effect="none">None</button>
        <button class="seg-btn${eff==='fade'?' active':''}" data-effect="fade">Fade</button>
        <button class="seg-btn${eff==='expand'?' active':''}" data-effect="expand">Expand</button>
      </div>
    </div>
  </div>
  <!-- SNAKE fields -->
  <div class="snake-fields" style="${isSnake?'':'display:none'}">
    <div class="field-row">
      <div class="field-label">Speed (px/sec on 1080p)</div>
      <div class="slider-row">
        <input class="f-snake-speed" type="range" min="10" max="1200" value="${+(a.snake_speed)||80}">
        <span class="sval f-snake-speed-v">${+(a.snake_speed)||80} px/s</span>
      </div>
    </div>
    <div class="field-row">
      <div class="field-label">Starting coverage</div>
      <div class="slider-row">
        <input class="f-snake-start" type="range" min="0" max="1" step="0.01" value="${+(a.snake_start)||0}">
        <span class="sval f-snake-start-v">${Math.round((+(a.snake_start)||0)*100)}%</span>
      </div>
    </div>
  </div>
  <div class="field-row" style="margin-top:2px">
    <div class="toggle-row">
      <span class="toggle-label field-label">Game Over style</span>
      <label class="toggle">
        <input class="f-game-over" type="checkbox" ${a.game_over?'checked':''}>
        <div class="ttrack"></div>
      </label>
    </div>
  </div>
  <button class="preview-btn octo" data-level="${levelNum}">▶ Preview</button>
</div>`;

  wireCard(card, idx);
  return card;
}

function wireCard(card, idx) {
  card.querySelector(".remove-btn").addEventListener("click", () => {
    if (state.alerts.length <= 1) return;
    state.alerts.splice(idx, 1);
    renderAlerts();
  });

  card.querySelectorAll(".mode-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      const isSnake = tab.dataset.mode === "snake";
      state.alerts[idx].snake_mode = isSnake;
      card.querySelectorAll(".mode-tab").forEach(t => t.classList.toggle("active", t===tab));
      card.querySelector(".perimeter-fields").style.display = isSnake ? "none" : "";
      card.querySelector(".snake-fields").style.display     = isSnake ? "" : "none";
    });
  });

  const minsInput = card.querySelector(".f-mins");
  minsInput.addEventListener("input", e => {
    const v = parseInt(e.target.value)||0;
    state.alerts[idx].minutes_before = v;
    card.querySelector(".f-mins-label").textContent =
      v===0 ? "At meeting start" : v===1 ? "1 minute before" : `${v} minutes before`;
  });

  const colorPicker = card.querySelector(".f-color");
  const colorHex    = card.querySelector(".f-hex");
  colorPicker.addEventListener("input", e => { state.alerts[idx].color = e.target.value; colorHex.value = e.target.value; });
  colorHex.addEventListener("change", e => {
    if (/^#[0-9a-fA-F]{6}$/.test(e.target.value)) {
      state.alerts[idx].color = e.target.value;
      colorPicker.value = e.target.value;
    }
  });

  const wSlider = card.querySelector(".f-width");
  const wVal    = card.querySelector(".f-width-v");
  const wMaxBtn = card.querySelector(".f-width-max");
  wSlider.addEventListener("input", e => {
    state.alerts[idx].width = +e.target.value;
    wVal.textContent = `${state.alerts[idx].width}px`;
    wMaxBtn.classList.remove("active");
  });
  wMaxBtn.addEventListener("click", () => {
    const isMax = wMaxBtn.classList.toggle("active");
    state.alerts[idx].width = isMax ? 2000 : +wSlider.value;
    wVal.textContent = isMax ? "Max" : `${+wSlider.value}px`;
  });

  const bSlider = card.querySelector(".f-blink");
  const bVal    = card.querySelector(".f-blink-v");
  bSlider.addEventListener("input", e => {
    state.alerts[idx].blink_hz = +e.target.value;
    bVal.textContent = `${state.alerts[idx].blink_hz.toFixed(1)} Hz`;
  });

  card.querySelectorAll(".seg-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      card.querySelectorAll(".seg-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const eff = btn.dataset.effect;
      state.alerts[idx].expand   = eff === "expand";
      state.alerts[idx].gradient = eff === "fade";
    });
  });

  const ssSlider = card.querySelector(".f-snake-speed");
  const ssVal    = card.querySelector(".f-snake-speed-v");
  ssSlider.addEventListener("input", e => {
    state.alerts[idx].snake_speed = +e.target.value;
    ssVal.textContent = `${state.alerts[idx].snake_speed} px/s`;
    // Reset preview so speed change is immediately visible
    const canvas = card.querySelector(".preview-canvas");
    if (canvas) canvas._startTime = performance.now()/1000;
  });

  const stSlider = card.querySelector(".f-snake-start");
  const stVal    = card.querySelector(".f-snake-start-v");
  stSlider.addEventListener("input", e => {
    state.alerts[idx].snake_start = +e.target.value;
    stVal.textContent = `${Math.round(state.alerts[idx].snake_start*100)}%`;
    const canvas = card.querySelector(".preview-canvas");
    if (canvas) canvas._startTime = performance.now()/1000;
  });

  card.querySelector(".f-game-over").addEventListener("change", e => {
    state.alerts[idx].game_over = e.target.checked;
  });

  card.querySelector(".preview-btn").addEventListener("click", async () => {
    const n = +card.querySelector(".preview-btn").dataset.level;
    try {
      const r = await fetch(`/api/preview/${n}`);
      const d = await r.json();
      d.ok ? toast(`▶ Level ${n} preview fired`) : toast(d.error||"Preview failed", true);
    } catch(e) { toast("Preview failed: "+e, true); }
  });
}

// ── Preview canvas ────────────────────────────────────────────────────────────
function initPreview(canvas, idx) {
  if (!canvas) return;
  const cw = canvas.parentElement.clientWidth - 16;
  canvas.width  = Math.max(60, cw);
  canvas.height = Math.round(canvas.width * REF_H / REF_W);
  canvas._startTime = performance.now()/1000;
  function frame(ts) {
    drawPreview(canvas, state.alerts[idx], ts/1000);
    rafs[idx] = requestAnimationFrame(frame);
  }
  rafs[idx] = requestAnimationFrame(frame);
}

function hexToRgb(hex) {
  const h = (hex||"#ff0000").replace("#","");
  return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
}

function drawPreview(canvas, a, t) {
  if (!a) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const scale = W / REF_W;
  const elapsed = Math.max(0, t - (canvas._startTime || t));

  // Base width (respect Max)
  const baseW = +a.width >= 1000
    ? Math.floor(W/2)
    : Math.max(1, Math.round((+a.width||40) * scale));

  let w = baseW;
  if (!a.snake_mode && a.expand) {
    // Width grows at 4px/sec (real pixels), scaled to canvas
    const extra = Math.round(elapsed * 4.0 * scale);
    w = Math.min(Math.floor(W/2), baseW + extra);
    if (w >= Math.floor(W/2)) { canvas._startTime = t; w = baseW; } // loop
  }

  ctx.clearRect(0,0,W,H);
  ctx.fillStyle = "#060303";
  ctx.fillRect(0,0,W,H);

  const [r,g,b] = hexToRgb(a.color);
  let alpha = 1.0;
  if (!a.snake_mode && (+a.blink_hz||0) > 0) {
    alpha = 0.4 + 0.6 * Math.abs(Math.sin(Math.PI * a.blink_hz * t));
  }

  if (a.snake_mode) {
    drawSnake(ctx, W, H, w, r, g, b, a, t, canvas._startTime||t);
  } else {
    drawBorder(ctx, W, H, w, r, g, b, alpha, !!a.gradient || !!a.expand);
  }
}

function drawBorder(ctx, W, H, w, r, g, b, alpha, gradient) {
  const color = `rgba(${r},${g},${b},${alpha})`;
  const clear  = `rgba(${r},${g},${b},0)`;
  const strips = [
    [0,   H-w, W, w,  0, H-w, 0, H],
    [0,   0,   W, w,  0, w,   0, 0],
    [0,   0,   w, H,  w, 0,   0, 0],
    [W-w, 0,   w, H,  W-w,0, W, 0],
  ];
  for (const [x,y,sw,sh,x0,y0,x1,y1] of strips) {
    if (gradient) {
      const g2 = ctx.createLinearGradient(x0,y0,x1,y1);
      g2.addColorStop(0, clear);
      g2.addColorStop(1, color);
      ctx.fillStyle = g2;
    } else {
      ctx.fillStyle = color;
    }
    ctx.fillRect(x,y,sw,sh);
  }
}

function drawSnake(ctx, W, H, w, r, g, b, a, t, startTime) {
  const hw = w/2;
  const segs = [
    [hw,   H-hw, W-hw, H-hw],
    [W-hw, H-hw, W-hw, hw],
    [W-hw, hw,   hw,   hw],
    [hw,   hw,   hw,   H-hw],
  ];
  const segLens = segs.map(([sx,sy,ex,ey]) => Math.hypot(ex-sx,ey-sy));
  const canvasPerimeter = segLens.reduce((a,b)=>a+b,0);
  const realPerimeter = 2*(REF_W-(+a.width||40)) + 2*(REF_H-(+a.width||40));
  const startFrac = +(a.snake_start)||0;
  const speed     = +(a.snake_speed)||80;
  const elapsed   = Math.max(0, t - startTime);
  // Loop so speed changes are always visible in preview
  const rawCov    = startFrac + elapsed * (speed / realPerimeter);
  const coverage  = rawCov % 1.0;
  const targetLen = coverage * canvasPerimeter;
  if (targetLen <= 0) return;
  ctx.strokeStyle = `rgb(${r},${g},${b})`;
  ctx.lineWidth   = w;
  ctx.lineCap     = "butt";
  ctx.lineJoin    = "miter";
  ctx.beginPath();
  let rem = targetLen, first = true;
  for (let i = 0; i < segs.length && rem > 0; i++) {
    const [sx,sy,ex,ey] = segs[i];
    if (first) { ctx.moveTo(sx,sy); first=false; }
    if (rem >= segLens[i]) { ctx.lineTo(ex,ey); rem -= segLens[i]; }
    else { const f=rem/segLens[i]; ctx.lineTo(sx+f*(ex-sx),sy+f*(ey-sy)); rem=0; }
  }
  ctx.stroke();
}

// ── Save ──────────────────────────────────────────────────────────────────────
document.getElementById("save-btn").addEventListener("click", async () => {
  const raw = document.getElementById("cals-input").value.trim();
  state.calendars = raw ? raw.split("\n").map(s=>s.trim()).filter(Boolean) : [];
  try {
    const r = await fetch("/api/config", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        calendars: state.calendars,
        alerts:    state.alerts,
        popup_font: state.popup_font,
        popup_pos:  state.popup_pos,
      }),
    });
    const d = await r.json();
    d.ok ? toast("✓ Saved — restart Hardstop to apply") : toast("Error: "+d.error, true);
  } catch(e) { toast("Save failed: "+e, true); }
});

// ── Toast ─────────────────────────────────────────────────────────────────────
let _tt = null;
function toast(msg, err=false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show" + (err?" err":"");
  if (_tt) clearTimeout(_tt);
  _tt = setTimeout(()=>{ el.className=""; }, 3200);
}

boot();
</script>
</body>
</html>"""





# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        print("AppKit not available. Install: pip install pyobjc-framework-Cocoa")
        sys.exit(1)

    APP_DIR.mkdir(parents=True, exist_ok=True)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    delegate = _AppDelegate.new()
    app.setDelegate_(delegate)
    print("Hardstop running in menu bar. Ctrl+C to quit.")
    app.run()


if __name__ == "__main__":
    main()
