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
    "alerts": [
        {
            "minutes_before": 5,
            "color": "#FF8C00",
            "width": 40,
            "blink_hz": 0.5,
            "expand": False,
            "expand_px": 0,
            "gradient": True,
            "snake_mode": False,
            "snake_speed": 80,
            "snake_start": 0.0,
        },
        {
            "minutes_before": 2,
            "color": "#FF4500",
            "width": 80,
            "blink_hz": 2.0,
            "expand": True,
            "expand_px": 120,
            "gradient": True,
            "snake_mode": False,
            "snake_speed": 160,
            "snake_start": 0.25,
        },
        {
            "minutes_before": 0,
            "color": "#FF0000",
            "width": 120,
            "blink_hz": 4.0,
            "expand": False,
            "expand_px": 0,
            "gradient": False,
            "snake_mode": False,
            "snake_speed": 320,
            "snake_start": 0.5,
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
      "30m"                          — 30 minutes from now
      "1h", "1h30m", "90m"          — duration from now
    """
    text = text.strip().lower().replace(" ", "")
    now = datetime.now().astimezone()

    # Duration with hours+minutes: "1h30m", "1h", "30m", "90m"
    m = re.fullmatch(r"(?:(\d+)h)?(\d+)m?", text)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        if hours or mins:
            return now + timedelta(hours=hours, minutes=mins)

    m = re.fullmatch(r"(\d+)h", text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

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

        use_gradient = cfg.get("gradient", False)
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

    def isOpaque(self):
        return False


# ── Info banner view ─────────────────────────────────────────────────────────

class _BannerView(NSView):
    """
    Pill at the top-center of the screen. Always shows Snooze + Dismiss.
    - Snooze: advances to the next (more urgent) alert level
    - Dismiss: clears the alert entirely
    """

    def initWithFrame_(self, frame):
        self = objc.super(_BannerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._label = ""
        self._start_dt = None
        self._cfg = None
        self._snooze_cb = None
        self._dismiss_cb = None
        self._snooze_rect = None
        self._dismiss_rect = None
        return self

    @objc.python_method
    def configure(self, label: str, start_dt, alert_cfg: dict,
                  snooze_cb, dismiss_cb) -> None:
        self._label = label
        self._start_dt = start_dt
        self._cfg = alert_cfg
        self._snooze_cb = snooze_cb
        self._dismiss_cb = dismiss_cb
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
            NSCenterTextAlignment, NSLeftTextAlignment,
        )
        from Foundation import NSMakeRect

        fw = self.frame().size.width
        fh = self.frame().size.height
        pad = 14.0
        radius = 14.0

        # Dark pill background
        NSColor.colorWithRed_green_blue_alpha_(0.05, 0.05, 0.05, 0.92).set()
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, fw, fh), radius, radius
        )
        pill.fill()

        # Colored border
        if self._cfg:
            r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF8C00"))
            NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.9).set()
            pill.setLineWidth_(1.5)
            pill.stroke()

        # Two buttons on the right: [Snooze] [Dismiss]
        btn_w, btn_h = 76.0, 28.0
        btn_gap = 8.0
        btn_y = (fh - btn_h) / 2
        dismiss_x = fw - pad - btn_w
        snooze_x = dismiss_x - btn_gap - btn_w
        text_w = snooze_x - pad - 8.0

        self._dismiss_rect = (dismiss_x, btn_y, btn_w, btn_h)
        self._snooze_rect  = (snooze_x,  btn_y, btn_w, btn_h)

        # Countdown
        status = ""
        if self._start_dt:
            secs = int((self._start_dt - datetime.now(tz=timezone.utc)).total_seconds())
            if secs > 0:
                m, s = divmod(secs, 60)
                status = f"in {m}:{s:02d}"
            else:
                status = "NOW"

        cps = NSMutableParagraphStyle.new()
        cps.setAlignment_(NSCenterTextAlignment)
        lps = NSMutableParagraphStyle.new()
        lps.setAlignment_(NSLeftTextAlignment)

        # Meeting label (bold)
        NSAttributedString.alloc().initWithString_attributes_(
            self._label,
            {
                NSFontAttributeName: NSFont.boldSystemFontOfSize_(15),
                NSForegroundColorAttributeName: NSColor.whiteColor(),
                NSParagraphStyleAttributeName: lps,
            },
        ).drawInRect_(NSMakeRect(pad, fh / 2 + 1, text_w, 20))

        # Status line
        NSAttributedString.alloc().initWithString_attributes_(
            status,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(11),
                NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.60, 1.0),
                NSParagraphStyleAttributeName: lps,
            },
        ).drawInRect_(NSMakeRect(pad, fh / 2 - 16, text_w, 16))

        # Draw buttons
        self._draw_btn("Snooze",  self._snooze_rect,  primary=False, cps=cps)
        self._draw_btn("Dismiss", self._dismiss_rect, primary=True,  cps=cps)

    @objc.python_method
    def _draw_btn(self, title, btn_rect, primary, cps):
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
            NSMakeRect(bx, by, bw, bh), 6, 6
        ).fill()

        NSAttributedString.alloc().initWithString_attributes_(
            title,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(12),
                NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.92, 1.0),
                NSParagraphStyleAttributeName: cps,
            },
        ).drawInRect_(NSMakeRect(bx + 2, by + 6, bw - 4, 18))

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

        sw = screen_frame.size.width
        sh = screen_frame.size.height
        bw, bh = 540.0, 66.0
        bx = (sw - bw) / 2
        by = sh - bh - 56

        # 128 = NSNonactivatingPanelMask: stays non-key, won't steal focus
        style = NSBorderlessWindowMask | 128

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
        view.configure(label, start_dt, cfg,
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

        alpha = (
            0.4 + 0.6 * abs(math.sin(math.pi * blink_hz * t))
            if blink_hz > 0 else 1.0
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
        elif cfg.get("expand") and cfg.get("expand_px", 0) > 0:
            elapsed = t - self._start_time
            extra = min(cfg["expand_px"], (elapsed / 60.0) * cfg["expand_px"])
            self._border_view.set_extra_width(extra)

        if self._banner_view:
            self._banner_view.setNeedsDisplay_(True)


# ── App delegate ─────────────────────────────────────────────────────────────

class _AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _notif):
        from AppKit import NSStatusBar, NSVariableStatusItemLength
        from Foundation import NSTimer

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
            dt = parse_hardstop_input(time_field.stringValue())
            if dt:
                save_hardstop(dt, name)
                print(f"Hardstop '{name}' set for {dt.astimezone().strftime('%-I:%M %p')}")
                self._refresh_hardstop_item()
                self._scheduler.reset_hardstop_alerts()
            else:
                err = NSAlert.new()
                err.setMessageText_("Couldn't parse the time. Try '4:55pm' or '30m'.")
                err.runModal()

    def clearHardstop_(self, _sender) -> None:
        clear_hardstop()
        self._scheduler.reset_hardstop_alerts()
        self._refresh_hardstop_item()

    def editConfig_(self, _sender) -> None:
        if not CONFIG_PATH.exists():
            load_config()  # creates default
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    def authorizeCalendar_(self, _sender) -> None:
        threading.Thread(target=authorize_calendar, daemon=True).start()

    def toggleLoginItem_(self, sender) -> None:
        enabled = not _login_item_enabled()
        _set_login_item(enabled)
        sender.setState_(1 if enabled else 0)


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
