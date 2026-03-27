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

# NSWindow level above screen saver (floats above all apps + full-screen spaces)
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
        },
        {
            "minutes_before": 2,
            "color": "#FF4500",
            "width": 80,
            "blink_hz": 2.0,
            "expand": True,
            "expand_px": 120,
            "gradient": True,
        },
        {
            "minutes_before": 0,
            "color": "#FF0000",
            "width": 120,
            "blink_hz": 4.0,
            "expand": False,
            "expand_px": 0,
            "gradient": False,
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
    """Load a cached OAuth token without opening a browser. Returns True on success."""
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
    """Run full OAuth flow (opens browser). Safe to call from a background thread."""
    global _calendar_service
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "Missing Google libraries.\n"
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )
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
    """Return list of (event_id, title, start_dt) for events in next 6 hours."""
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

def load_hardstop() -> datetime | None:
    if not HARDSTOP_PATH.exists():
        return None
    try:
        data = json.loads(HARDSTOP_PATH.read_text())
        dt = datetime.fromisoformat(data["time"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def save_hardstop(dt: datetime) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    HARDSTOP_PATH.write_text(json.dumps({"time": dt.isoformat()}))


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
    now = datetime.now().astimezone()  # local time, tz-aware

    # Duration: e.g. "30m", "1h", "1h30m", "90m"
    m = re.fullmatch(r"(?:(\d+)h)?(\d+)m?", text)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        if hours or mins:
            return now + timedelta(hours=hours, minutes=mins)

    # Also match bare hours: "2h"
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
    """
    Polls calendar events and the manual hardstop, firing callbacks when an
    alert threshold is crossed. Deduplicates by (event_id, minutes_before).
    """

    def __init__(self, config: dict, on_alert):
        self._config = config
        self._on_alert = on_alert  # fn(label, start_dt, alert_cfg)
        self._fired: set = set()
        self._lock = threading.Lock()

    def reset_hardstop_alerts(self) -> None:
        with self._lock:
            self._fired = {k for k in self._fired if k[0] != "__hardstop__"}

    def poll(self) -> list[tuple[str, datetime]]:
        """
        Check events and hardstop. Fire callbacks as needed.
        Returns list of (label, start_dt) for upcoming events (for menu display).
        """
        now = datetime.now(tz=timezone.utc)
        events = _fetch_upcoming_events(self._config.get("calendars", []))

        # Inject manual hardstop as a synthetic event
        hs = load_hardstop()
        if hs:
            if now > hs + timedelta(minutes=5):
                clear_hardstop()
            else:
                events.append(("__hardstop__", "Hardstop", hs))

        # Sort alerts descending so we fire the most urgent one if multiple overlap
        alerts = sorted(
            self._config.get("alerts", []),
            key=lambda a: a["minutes_before"],
            reverse=True,
        )

        for event_id, label, start_dt in events:
            for alert in alerts:
                mins = alert["minutes_before"]
                target = start_dt - timedelta(minutes=mins)
                key = (event_id, mins)
                with self._lock:
                    if key in self._fired:
                        continue
                    if abs(now - target) <= timedelta(seconds=45):
                        self._fired.add(key)
                        self._on_alert(label, start_dt, alert)

        upcoming = [(lbl, dt) for _, lbl, dt in events if dt > now]
        upcoming.sort(key=lambda x: x[1])
        return upcoming


# ── Border overlay view ──────────────────────────────────────────────────────

import objc
from Foundation import NSObject
from AppKit import NSView


class _BorderView(NSView):
    """
    Transparent full-screen view that draws a colored border around screen edges.
    The center is completely clear so the user can keep working normally.
    """

    def initWithFrame_(self, frame):
        self = objc.super(_BorderView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._cfg = None
        self._extra_width = 0.0
        return self

    @objc.python_method
    def configure(self, alert_cfg: dict) -> None:
        self._cfg = alert_cfg
        self._extra_width = 0.0
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_extra_width(self, w: float) -> None:
        self._extra_width = w
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if not self._cfg:
            return

        from AppKit import NSColor, NSBezierPath, NSGradient
        from Foundation import NSMakeRect

        cfg = self._cfg
        r, g, b = _hex_to_rgb(cfg.get("color", "#FF0000"))
        use_gradient = cfg.get("gradient", False)
        w = cfg.get("width", 40) + self._extra_width
        fw = self.frame().size.width
        fh = self.frame().size.height

        color = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)
        clear = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.0)

        # 4 border strips with gradient angles.
        # NSGradient angle convention: 0°=left→right, 90°=bottom→top,
        #   180°=right→left, 270°=top→bottom.  Colors are [startColor, endColor].
        # We use [clear, color] so each strip fades from transparent at center
        # to solid at the screen edge.
        strips = [
            (NSMakeRect(0, fh - w, fw, w), 90),   # top:   bottom=clear → top=color
            (NSMakeRect(0, 0, fw, w),      270),   # bottom: top=clear → bottom=color
            (NSMakeRect(0, 0, w, fh),      180),   # left:  right=clear → left=color
            (NSMakeRect(fw - w, 0, w, fh), 0),     # right: left=clear → right=color
        ]

        for strip, angle in strips:
            if use_gradient:
                gradient = NSGradient.alloc().initWithColors_([clear, color])
                gradient.drawInRect_angle_(strip, angle)
            else:
                color.set()
                NSBezierPath.fillRect_(strip)

    def isOpaque(self):
        return False


# ── Info banner view ─────────────────────────────────────────────────────────

class _BannerView(NSView):
    """
    Small pill displayed at the top-center of the screen showing meeting info
    and a Dismiss button. The border overlay behind it is click-through, but
    this banner receives mouse events normally.
    """

    def initWithFrame_(self, frame):
        self = objc.super(_BannerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._label = ""
        self._start_dt = None
        self._cfg = None
        self._dismiss_cb = None
        self._btn_rect = None
        return self

    @objc.python_method
    def configure(self, label: str, start_dt, alert_cfg: dict, dismiss_cb) -> None:
        self._label = label
        self._start_dt = start_dt
        self._cfg = alert_cfg
        self._dismiss_cb = dismiss_cb
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        from AppKit import (
            NSColor, NSBezierPath, NSAttributedString, NSFont,
            NSMutableParagraphStyle, NSForegroundColorAttributeName,
            NSFontAttributeName, NSParagraphStyleAttributeName,
        )
        from AppKit import NSCenterTextAlignment
        from Foundation import NSMakeRect

        fw = self.frame().size.width
        fh = self.frame().size.height
        pad = 14.0
        radius = 14.0
        mins = self._cfg.get("minutes_before", 0) if self._cfg else 0
        show_btn = (mins <= 2)

        # Dark pill background
        NSColor.colorWithRed_green_blue_alpha_(0.05, 0.05, 0.05, 0.90).set()
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, fw, fh), radius, radius
        )
        pill.fill()

        # Colored border matching alert
        if self._cfg:
            r, g, b = _hex_to_rgb(self._cfg.get("color", "#FF8C00"))
            NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.85).set()
            pill.setLineWidth_(1.5)
            pill.stroke()

        ps = NSMutableParagraphStyle.new()
        ps.setAlignment_(NSCenterTextAlignment)

        # Compute dismiss button area
        btn_w = 72.0 if show_btn else 0.0
        btn_gap = 10.0 if show_btn else 0.0
        text_w = fw - 2 * pad - btn_w - btn_gap
        text_x = pad

        # Countdown
        if self._start_dt:
            now = datetime.now(tz=timezone.utc)
            secs = int((self._start_dt - now).total_seconds())
            if secs > 0:
                m, s = divmod(secs, 60)
                status = f"in {m}:{s:02d}"
            else:
                status = "NOW"
        else:
            status = ""

        # Meeting label
        label_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(15),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
            NSParagraphStyleAttributeName: ps,
        }
        label_str = NSAttributedString.alloc().initWithString_attributes_(
            self._label, label_attrs
        )
        label_str.drawInRect_(NSMakeRect(text_x, fh / 2 + 1, text_w, 20))

        # Status line
        status_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11),
            NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.65, 1.0),
            NSParagraphStyleAttributeName: ps,
        }
        status_str = NSAttributedString.alloc().initWithString_attributes_(
            status, status_attrs
        )
        status_str.drawInRect_(NSMakeRect(text_x, fh / 2 - 16, text_w, 16))

        # Dismiss button
        if show_btn:
            btn_x = fw - btn_w - pad
            btn_y = (fh - 26) / 2
            self._btn_rect = (btn_x, btn_y, btn_w, 26)

            NSColor.colorWithWhite_alpha_(1.0, 0.12).set()
            btn_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(btn_x, btn_y, btn_w, 26), 6, 6
            )
            btn_path.fill()

            dismiss_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(12),
                NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(0.9, 1.0),
                NSParagraphStyleAttributeName: ps,
            }
            NSAttributedString.alloc().initWithString_attributes_(
                "Dismiss", dismiss_attrs
            ).drawInRect_(NSMakeRect(btn_x + 2, btn_y + 5, btn_w - 4, 18))
        else:
            self._btn_rect = None

    def mouseDown_(self, event):
        pass  # Accept event so mouseUp_ fires

    def mouseUp_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        lx, ly = loc.x, loc.y
        mins = self._cfg.get("minutes_before", 0) if self._cfg else 0

        if self._btn_rect:
            bx, by, bw, bh = self._btn_rect
            if bx <= lx <= bx + bw and by <= ly <= by + bh:
                if self._dismiss_cb:
                    self._dismiss_cb()
                return

        # Click anywhere dismisses the 5-min alert (no button needed)
        if mins >= 5 and self._dismiss_cb:
            self._dismiss_cb()

    def acceptsFirstMouse_(self, event):
        return True

    def isOpaque(self):
        return False


# ── Overlay controller ───────────────────────────────────────────────────────

class OverlayController:
    """
    Manages the border window and info banner. All methods must be called on
    the main (AppKit) thread.
    """

    def __init__(self):
        self._border_win = None
        self._border_view = None
        self._banner_win = None
        self._banner_view = None
        self._timer = None
        self._start_time: float = 0.0
        self._current_cfg: dict | None = None

    @property
    def is_active(self) -> bool:
        return self._current_cfg is not None

    def show(self, label: str, start_dt, alert_cfg: dict, tick_target) -> None:
        """Display overlay for alert_cfg. Replaces any existing overlay."""
        self._teardown()
        self._current_cfg = alert_cfg
        self._start_time = time.time()

        from AppKit import NSScreen
        frame = NSScreen.mainScreen().frame()

        self._make_border(frame, alert_cfg)
        self._make_banner(frame, label, start_dt, alert_cfg)

        from Foundation import NSTimer
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30.0, tick_target, "overlayTick:", None, True
        )

    def dismiss(self) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._border_win:
            self._border_win.orderOut_(None)
            self._border_win = None
            self._border_view = None
        if self._banner_win:
            self._banner_win.orderOut_(None)
            self._banner_win = None
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

        self._border_win = win
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
        bw, bh = 460.0, 66.0
        bx = (sw - bw) / 2
        by = sh - bh - 56

        # NSNonactivatingPanelMask = 128; avoids stealing focus
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
        view.configure(label, start_dt, cfg, self._teardown)
        banner.setContentView_(view)
        banner.orderFrontRegardless()

        self._banner_win = banner
        self._banner_view = view

    def tick(self) -> None:
        """Called at 30fps by the NSTimer. Drives blink and expand animation."""
        if not self._border_win or not self._current_cfg:
            return

        cfg = self._current_cfg
        t = time.time()
        blink_hz = cfg.get("blink_hz", 0)

        alpha = (
            0.4 + 0.6 * abs(math.sin(math.pi * blink_hz * t))
            if blink_hz > 0
            else 1.0
        )
        self._border_win.setAlphaValue_(alpha)

        if cfg.get("expand") and cfg.get("expand_px", 0) > 0:
            elapsed = t - self._start_time
            extra = min(cfg["expand_px"], (elapsed / 60.0) * cfg["expand_px"])
            self._border_view.set_extra_width(extra)

        if self._banner_view:
            self._banner_view.setNeedsDisplay_(True)

        # Auto-dismiss 5-min alerts after 30 seconds
        if cfg.get("minutes_before", 0) >= 5 and (t - self._start_time) > 30:
            self._teardown()


# ── App delegate ─────────────────────────────────────────────────────────────

class _AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _notif):
        from AppKit import (
            NSStatusBar, NSVariableStatusItemLength,
            NSControlStateValueOn, NSControlStateValueOff,
        )
        from Foundation import NSTimer

        self._config = load_config()
        self._overlay = OverlayController()
        self._upcoming: list[tuple[str, datetime]] = []
        self._pending_alert: tuple | None = None

        # Scheduler (calendar + hardstop) runs in background
        self._scheduler = AlertScheduler(self._config, self._on_alert_from_thread)
        threading.Thread(target=self._poll_loop, daemon=True).start()

        # Try to restore cached Google token without blocking startup
        threading.Thread(target=_try_load_cached_token, daemon=True).start()

        # Status bar item
        bar = NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        item.button().setImage_(_make_octagon_icon(filled=False))
        item.button().setToolTip_("Hardstop")
        self._status_item = item  # must retain

        self._build_menu()

        # Refresh menu label every 30s
        self._menu_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            30, self, "refreshMenuLabels:", None, True
        )

    # ── Menu construction ────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        from AppKit import NSMenu, NSMenuItem

        menu = NSMenu.new()

        # Next event
        self._next_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            self._next_event_label(), None, ""
        )
        self._next_item.setEnabled_(False)
        menu.addItem_(self._next_item)

        # Active hardstop (hidden when none set)
        self._hs_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "", "clearHardstop:", ""
        )
        self._hs_item.setTarget_(self)
        menu.addItem_(self._hs_item)
        self._refresh_hardstop_item()

        menu.addItem_(NSMenuItem.separatorItem())

        # Set Hardstop
        si = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Set Hardstop…", "setHardstop:", ""
        )
        si.setTarget_(self)
        menu.addItem_(si)

        menu.addItem_(NSMenuItem.separatorItem())

        # Google Calendar auth
        self._auth_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            self._auth_label(), "authorizeCalendar:", ""
        )
        self._auth_item.setTarget_(self)
        menu.addItem_(self._auth_item)

        menu.addItem_(NSMenuItem.separatorItem())

        # Open at login
        self._login_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open at Login", "toggleLoginItem:", ""
        )
        self._login_item.setTarget_(self)
        from AppKit import NSControlStateValueOn, NSControlStateValueOff
        self._login_item.setState_(
            NSControlStateValueOn if _login_item_enabled() else NSControlStateValueOff
        )
        menu.addItem_(self._login_item)

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", ""
        )
        quit_item.setImage_(None)  # Strip macOS Tahoe auto-symbol
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    def _next_event_label(self) -> str:
        if self._upcoming:
            label, dt = self._upcoming[0]
            now = datetime.now(tz=timezone.utc)
            mins = max(0, int((dt - now).total_seconds() / 60))
            return f"Next: {label} in {mins}m"
        return "No upcoming events"

    def _auth_label(self) -> str:
        with _calendar_lock:
            has_svc = _calendar_service is not None
        return "Re-authorize Calendar" if has_svc else "Authorize Google Calendar"

    def _refresh_hardstop_item(self) -> None:
        hs = load_hardstop()
        if hs:
            local = hs.astimezone()
            time_str = local.strftime("%-I:%M %p")
            self._hs_item.setTitle_(f"⏹ Hardstop: {time_str}  —  Clear")
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

    # ── Background poll loop ─────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while True:
            try:
                upcoming = self._scheduler.poll()
                self._upcoming = upcoming
            except Exception as e:
                print(f"Poll error: {e}")
            time.sleep(60)

    # ── Alert callback (from background thread → main thread) ────────────────

    def _on_alert_from_thread(self, label: str, start_dt, alert_cfg: dict) -> None:
        self._pending_alert = (label, start_dt, alert_cfg)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showPendingAlert:", None, False
        )

    def showPendingAlert_(self, _) -> None:
        if not self._pending_alert:
            return
        label, start_dt, cfg = self._pending_alert
        self._pending_alert = None
        self._overlay.show(label, start_dt, cfg, self)
        self._status_item.button().setImage_(_make_octagon_icon(filled=True))

    def overlayTick_(self, _timer) -> None:
        self._overlay.tick()
        if not self._overlay.is_active:
            self._status_item.button().setImage_(_make_octagon_icon(filled=False))

    # ── Menu actions ─────────────────────────────────────────────────────────

    def setHardstop_(self, _sender) -> None:
        from AppKit import NSAlert, NSTextField
        from Foundation import NSMakeRect

        alert = NSAlert.new()
        alert.setMessageText_("Set Hardstop")
        alert.setInformativeText_(
            'Enter a time ("4:55pm", "16:55") or duration ("30m", "1h30m"):'
        )
        alert.addButtonWithTitle_("Set")
        alert.addButtonWithTitle_("Cancel")

        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 24))
        field.setPlaceholderString_("e.g.  4:55pm  or  30m  or  1h30m")
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)

        NSAlertFirstButtonReturn = 1000
        if alert.runModal() == NSAlertFirstButtonReturn:
            dt = parse_hardstop_input(field.stringValue())
            if dt:
                save_hardstop(dt)
                local_str = dt.astimezone().strftime("%-I:%M %p")
                print(f"Hardstop set for {local_str}")
                self._refresh_hardstop_item()
                self._scheduler.reset_hardstop_alerts()
            else:
                err = NSAlert.new()
                err.setMessageText_("Couldn't parse that. Try '4:55pm' or '30m'.")
                err.runModal()

    def clearHardstop_(self, _sender) -> None:
        clear_hardstop()
        self._refresh_hardstop_item()

    def authorizeCalendar_(self, _sender) -> None:
        threading.Thread(target=authorize_calendar, daemon=True).start()

    def toggleLoginItem_(self, sender) -> None:
        from AppKit import NSControlStateValueOn, NSControlStateValueOff
        enabled = not _login_item_enabled()
        _set_login_item(enabled)
        sender.setState_(
            NSControlStateValueOn if enabled else NSControlStateValueOff
        )


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        print(
            "AppKit not available.\n"
            "Install dependencies: pip install pyobjc-framework-Cocoa"
        )
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
