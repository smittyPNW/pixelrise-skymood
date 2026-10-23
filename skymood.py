#!/usr/bin/env python3
"""Sky-mood controller for the Dekala PixelRise clock (local Tuya 3.5, no cloud).

Every few minutes it reads NOAA SWPC space weather (1-minute Kp, the 3-day Kp
forecast, solar-wind Bz, OVATION aurora probability) plus the sun's altitude at
home, picks a mood (day / night / aurora_watch / aurora_storm) and, only when the
mood changes, rewrites the clock's built-in animation playlist (DP 121), clock
text colour and backlight (DP 11). Optional: an aurora wake-up on Alarm 1
(DP 111 + DP 16), restored afterwards.

  .venv/bin/python -I skymood.py status            sky + chosen mood, no device access
  .venv/bin/python -I skymood.py once [--dry-run]  one evaluation (+ write if mood changed)
  .venv/bin/python -I skymood.py run  [--dry-run]  daemon: holds the local connection
  .venv/bin/python -I skymood.py preview MOOD [SECONDS]  show a mood, then restore the day playlist

Config: config.local.json (git-ignored; see config.example.json).
State:  state/state.json (git-ignored). Key: secrets/devices.json (get_keys.py).
"""
import base64
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state", "state.json")
LOG = os.path.join(ROOT, "state", "skymood.log")
SWPC = "https://services.swpc.noaa.gov/"

# DPs decoded from captures (FINDINGS.md section 6)
DP_BACKLIGHT, DP_ALARM1_ON, DP_ALARM1, DP_PLAYLIST = 11, 16, 111, 121
COLORS = {"white": 0, "red": 1, "orange": 2, "yellow": 3, "green": 4, "cyan": 5, "blue": 6, "purple": 7}
FACES = {11, 12}  # clock faces take a text colour


# ----------------------------------------------------------------- utilities
def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


KIND_ORDER = ["think", "read", "delegate", "run", "write", "wait"]   # later wins when sessions differ
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
RUN_TOOLS = {"Bash", "PowerShell", "Monitor"}
DELEGATE_TOOLS = {"Agent", "Task", "Workflow", "SendMessage"}
READ_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "LS", "ToolSearch"}


def tool_kind(tool):
    if tool in WRITE_TOOLS:
        return "write"
    if tool in RUN_TOOLS or any(w in tool for w in ("computer", "click", "navigate", "preview_start")):
        return "run"
    if tool in DELEGATE_TOOLS or "start_session" in tool:
        return "delegate"
    t = tool.lower()
    if tool in READ_TOOLS or any(w in t for w in ("read", "search", "fetch", "find", "list", "get", "screenshot", "zoom")):
        return "read"
    return "think"  # planning tools, wrap-ups, anything else


def claude_activity(cfg):
    """What each busy Claude Code session is doing, e.g. ["write", "read"]; [] when none.

    Hooks write one line into <busy_dir>/<session_id>:
      think | busy        prompt submitted (UserPromptSubmit)
      tool <ToolName>     a tool started or finished (Pre/PostToolUse)
      wait                waiting on a permission prompt (Notification)
      idle                turn over (Stop / StopFailure / SessionEnd)
    A non-idle file untouched for stale_hours is a crashed session; idle files
    older than a day are pruned here."""
    c = cfg.get("claude") or {}
    d = os.path.expanduser(c.get("busy_dir", "~/Library/Application Support/skymood/claude-busy"))
    now, kinds = time.time(), []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return []
    for e in entries:
        try:
            age = now - e.stat().st_mtime
            with open(e.path) as f:
                parts = f.read(120).split()
        except OSError:
            continue
        state = parts[0] if parts else "idle"
        if state != "idle" and age < c.get("stale_hours", 2) * 3600:
            if state == "tool" and len(parts) > 1:
                kinds.append(tool_kind(parts[1]))
            elif state == "wait":
                kinds.append("wait")
            else:
                kinds.append("think")
        elif state == "idle" and age > 86400:
            try:
                os.remove(e.path)
            except OSError:
                pass
    return kinds


def claude_scene(cfg, kinds):
    """(key, playlist items) for the current activity; kinds=[] means "just finished"."""
    c = cfg["claude"]
    sc = c["scenes"]
    if not kinds:
        key, anims, secs = ("done", 0), list(sc["done"]), 15
    else:
        kind = max(kinds, key=KIND_ORDER.index)
        level = min(len(kinds), 3)
        anims = list(sc[kind])
        if kind != "wait":
            anims += sc.get(f"busy{level}", []) if level > 1 else []
        key, secs = (kind, level), {1: 20, 2: 15, 3: 10}[level]
    tint = {int(k): v for k, v in (c.get("item_colors") or {}).items()}  # e.g. {"52": "orange"}
    items = [dict({"anim": a, "seconds": secs}, **({"color": tint[a]} if a in tint else {})) for a in dict.fromkeys(anims)]
    tc = c.get("time_check")
    if tc:
        items.append(dict(tc))
    return key, items


def fetch(path):
    req = urllib.request.Request(SWPC + path, headers={"User-Agent": "skymood/1 (personal clock)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- sky data
def sun_altitude(lat, lon, t):
    """Solar altitude in degrees (low-precision almanac, ~0.5 deg)."""
    n = t.timestamp() / 86400 + 2440587.5 - 2451545.0
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians((280.460 + 0.9856474 * n) % 360 + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    ha = math.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def kp1m_valid(rows):
    """Drop SWPC's exact-zero placeholder cliffs (a 0.0 right after a reading >= 1.33).

    Z-suffixed rows are real whole-number Kp, not placeholders."""
    out, last = [], None
    for r in rows:
        v = r.get("estimated_kp")
        if v is None:
            continue
        t = utc(r["time_tag"])
        if v == 0 and r.get("kp_index") == 0 and last and last[1] >= 1.33 and t - last[0] <= timedelta(hours=3):
            continue
        out.append((t, v))
        last = (t, v)
    return out


def ovation_view_prob(grid, lat, lon, reach=10):
    """Max OVATION probability from home latitude to `reach` degrees poleward
    (aurora that far north is still visible low on the horizon)."""
    lon360 = round(lon % 360)
    lons = {(lon360 + d) % 360 for d in (-2, -1, 0, 1, 2)}
    best = 0
    for glon, glat, p in grid:
        if glon in lons and lat <= glat <= lat + reach:
            best = max(best, p)
    return best


def read_sky(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    lat, lon = cfg["lat"], cfg["lon"]
    sky = {"time": now.isoformat(timespec="seconds"), "sun_alt": round(sun_altitude(lat, lon, now), 1)}
    try:
        rows = kp1m_valid(fetch("json/planetary_k_index_1m.json"))
        recent = [v for t, v in rows if now - t <= timedelta(minutes=30)]
        sky["kp_now"] = round(max(recent), 2) if recent else None
    except Exception as e:
        sky["kp_now"] = None
        sky["kp_error"] = str(e)
    try:
        dark = [now + timedelta(minutes=15 * i) for i in range(4 * 18)]
        dark = [t for t in dark if sun_altitude(lat, lon, t) < cfg["dark_sun_alt"]]
        allrows = fetch("products/noaa-planetary-k-index-forecast.json")
        fc = [r for r in allrows if r.get("observed") in ("predicted", "estimated")]
        tonight = [r["kp"] for r in fc if any(utc(r["time_tag"]) <= t < utc(r["time_tag"]) + timedelta(hours=3) for t in dark)]
        sky["kp_tonight_max"] = max(tonight) if tonight else None
        # the official 3-hour Kp for the block we're in (what "it hit Kp 5" refers to)
        block = [r["kp"] for r in allrows if r.get("observed") in ("observed", "estimated")
                 and utc(r["time_tag"]) <= now < utc(r["time_tag"]) + timedelta(hours=3)]
        sky["kp_block"] = block[-1] if block else None
    except Exception as e:
        sky["kp_tonight_max"] = sky["kp_block"] = None
        sky["forecast_error"] = str(e)
    sky["kp"] = max(v for v in (sky.get("kp_now"), sky.get("kp_block"), 0) if v is not None)
    try:
        sky["bz"] = fetch("products/summary/solar-wind-mag-field.json")[0].get("bz_gsm")
    except Exception as e:
        sky["bz"] = None
        sky["bz_error"] = str(e)
    try:
        grid = fetch("json/ovation_aurora_latest.json")["coordinates"]
        th = cfg["thresholds"]
        sky["ovation_view"] = ovation_view_prob(grid, lat, lon, th.get("watch_reach_deg", 10))
        sky["ovation_near"] = ovation_view_prob(grid, lat, lon, th.get("storm_reach_deg", 6))
    except Exception as e:
        sky["ovation_view"] = sky["ovation_near"] = None
        sky["ovation_error"] = str(e)
    return sky


def decide(sky, cfg, now=None):
    t = cfg["thresholds"]
    dark = sky["sun_alt"] < cfg["dark_sun_alt"]
    kp = sky.get("kp", sky.get("kp_now")) or 0
    view = sky.get("ovation_view") or 0
    near = sky.get("ovation_near", view) or 0
    bz = sky.get("bz")
    tonight = sky.get("kp_tonight_max") or 0
    if dark and (kp >= t["storm_kp"] or near >= t["storm_view_prob"]):
        return "aurora_storm"
    if dark and (kp >= t["watch_kp"] or view >= t["watch_view_prob"] or tonight >= t["storm_kp"]
                 or (bz is not None and bz <= t["watch_bz"])):
        return "aurora_watch"
    nightly = in_night_hours(cfg, now)
    if nightly is None:
        nightly = sky["sun_alt"] < cfg["night_sun_alt"]
    return "night" if nightly else "day"


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_night_hours(cfg, now=None):
    """True inside cfg["night_hours"] = ["20:00", "07:00"] (local time, may wrap midnight);
    None when no schedule is configured (then dusk/dawn decides)."""
    nh = cfg.get("night_hours")
    if not nh:
        return None
    now = now or datetime.now()
    m, start, end = now.hour * 60 + now.minute, _minutes(nh[0]), _minutes(nh[1])
    return start <= m < end if start < end else (m >= start or m < end)


def seconds_to_night_boundary(cfg, now=None):
    nh = cfg.get("night_hours")
    if not nh:
        return None
    now = now or datetime.now()
    waits = []
    for hhmm in nh:
        h, m = map(int, hhmm.split(":"))
        b = now.replace(hour=h, minute=m, second=2, microsecond=0)
        if b <= now:
            b += timedelta(days=1)
        waits.append((b - now).total_seconds())
    return min(waits)


# ----------------------------------------------------------------- clock
def active_season(cfg, today=None):
    """The seasonal daytime pack whose "from".."to" (MM-DD, may wrap the new year) covers today."""
    md = (today or datetime.now()).strftime("%m-%d")
    for s in cfg.get("seasons", []):
        a, b = s["from"], s["to"]
        if (a <= md <= b) if a <= b else (md >= a or md <= b):
            return s
    return None


def kp_color(kp):
    """Clock colour for the live Kp, following NOAA's G-scale: G1 starts at Kp 5."""
    for limit, name in ((5, "green"), (6, "yellow"), (7, "orange"), (8, "red")):
        if kp < limit:
            return name
    return "purple"


def encode_item(it):
    """One 11-byte DP 121 entry, laid out exactly as the app writes them."""
    anim, dur = int(it["anim"]), int(it.get("seconds", 30))
    color = COLORS.get(it.get("color", "white"), 0)
    if anim in FACES:
        return bytes([anim, 0, 0, 0, 0, 0, 0, 0, dur, color, 0])
    return bytes([anim, 1, 1, 1, 1, 1, 5, 0, dur, color, 0x32])


def encode_playlist(items):
    if not 1 <= len(items) <= 19:
        raise ValueError("the clock takes 1-19 playlist items")
    return base64.b64encode(b"".join(encode_item(i) for i in items)).decode()


def alarm_bytes(saved_b64, hour, minute):
    b = bytearray(base64.b64decode(saved_b64))
    b[0], b[1] = hour, minute
    return base64.b64encode(bytes(b)).decode()


class Clock:
    def __init__(self, cfg, persist):
        import tinytuya
        dev = next(d for d in load_json(os.path.join(ROOT, "secrets", "devices.json"), [])
                   if d.get("name") == cfg["device_name"])
        self.d = tinytuya.Device(dev["id"], cfg["clock_ip"], dev["local_key"], version=3.5, persist=persist)
        if persist:
            self.d.set_socketPersistent(True)

    def status(self):
        return (self.d.status() or {}).get("dps", {})

    def set(self, dp, value):
        r = self.d.set_value(dp, value)
        if not r or "Error" in r:
            raise RuntimeError(f"write DP {dp} failed: {r}")
        return r

    def receive(self):
        return self.d.receive()

    def heartbeat(self):
        self.d.heartbeat(nowait=True)


# ----------------------------------------------------------------- controller
class Controller:
    def __init__(self, cfg, dry_run, persist):
        self.cfg, self.dry = cfg, dry_run
        self.state = load_json(STATE, {})
        self.state.setdefault("baseline_playlist_b64", cfg["baseline_playlist_b64"])
        self.state.setdefault("baseline_backlight", cfg["baseline_backlight"])
        self.state.setdefault("alarm1_saved_b64", cfg["alarm1_saved_b64"])
        self.clock = None if dry_run else Clock(cfg, persist)
        self.claude_key, self.claude_items, self.claude_since, self.claude_last_busy = None, None, 0.0, 0.0

    def playlist_for(self, mood):
        if mood == "day":
            season = active_season(self.cfg)
            if season:
                return encode_playlist(season["playlist"]), self.state["baseline_backlight"]
            return self.state["baseline_playlist_b64"], self.state["baseline_backlight"]
        m = self.cfg["moods"][mood]
        kp_col = kp_color(self.state.get("kp", 0))  # "color": "kp" = clock tinted by the live Kp
        items = [dict(it, color=kp_col) if it.get("color") == "kp" else it for it in m["playlist"]]
        return encode_playlist(items), m["backlight"]

    def target(self):
        """What the clock should show: aurora moods win, then the Claude scene
        (at the current mood's brightness), then the sky mood."""
        mood = self.state.get("mood") or "day"
        if mood.startswith("aurora") or not self.claude_key:
            pl, bl = self.playlist_for(mood)
        else:
            pl, bl = encode_playlist(self.claude_items), self.playlist_for(mood)[1]
        if in_night_hours(self.cfg):
            bl = "night"  # quiet hours: always dim, even for aurora and Claude scenes
        return pl, bl

    def apply(self, why):
        if self.state.get("paused_until", 0) > time.time():
            return
        pl, bl = self.target()
        if pl != self.state.get("shown_playlist"):
            self.write(DP_PLAYLIST, pl, why)
            self.state["shown_playlist"] = pl
        if bl != self.state.get("shown_backlight"):
            self.write(DP_BACKLIGHT, bl, f"backlight {bl}")
            self.state["shown_backlight"] = bl

    def update_claude(self):
        """Pick the Claude scene from what the sessions are doing; hold each scene
        at least min_dwell_seconds unless Claude needs you, finishes, or stops."""
        c = self.cfg.get("claude") or {}
        if not c.get("enabled"):
            return
        now = time.time()
        kinds = claude_activity(self.cfg)
        if kinds:
            self.claude_last_busy = now
            key, items = claude_scene(self.cfg, kinds)
        elif now - self.claude_last_busy < c.get("linger_seconds", 60):
            key, items = claude_scene(self.cfg, [])
        else:
            key, items = None, None
        if self.state.get("mood") == "night" and not c.get("at_night", True):
            key, items = None, None
        if key == self.claude_key:
            return
        urgent = (key is None or self.claude_key is None or key[0] in ("wait", "done")
                  or self.claude_key[0] == "wait")
        if not urgent and now - self.claude_since < c.get("min_dwell_seconds", 45):
            return
        self.claude_key, self.claude_items, self.claude_since = key, items, now
        self.apply(f"Claude {key[0]} x{key[1]}" if key else "Claude Code idle")
        if not self.dry:
            save_json(STATE, self.state)

    def write(self, dp, value, why):
        log(f"{'DRY-RUN ' if self.dry else ''}write DP {dp} ({why})")
        if not self.dry:
            self.clock.set(dp, value)
        if dp == DP_PLAYLIST:
            self.state["last_written_playlist"] = value

    def evaluate(self):
        sky = read_sky(self.cfg)
        self.state["kp"] = sky.get("kp", 0)
        mood = decide(sky, self.cfg)
        cur = self.state.get("mood")
        rank = ["day", "night", "aurora_watch", "aurora_storm"]
        now = time.time()
        if self.state.get("paused_until", 0) > now:
            log(f"sky {json.dumps(sky)} -> {mood} (paused: manual change in the app)")
            return sky, mood
        # escalate at once; step down only after the calmer mood has held a while
        if cur in ("aurora_watch", "aurora_storm") and mood != cur and rank.index(mood) < rank.index(cur):
            since = self.state.setdefault("calmer_since", now)
            if now - since < self.cfg["step_down_minutes"] * 60:
                log(f"sky {json.dumps(sky)} -> {mood} (holding {cur} for step-down delay)")
                return sky, cur
        self.state.pop("calmer_since", None)
        log(f"sky {json.dumps(sky)} -> {mood}")
        self.state["mood"] = mood
        self.apply(f"mood {cur} -> {mood}")
        self.aurora_alarm(mood)
        if not self.dry:
            save_json(STATE, self.state)
        return sky, mood

    def aurora_alarm(self, mood):
        if not self.cfg.get("aurora_alarm"):
            return
        a = self.state.get("alarm")
        now = datetime.now()
        night = (now - timedelta(hours=12)).strftime("%Y-%m-%d")  # one wake-up per night, across midnight
        if mood == "aurora_storm" and not a and self.state.get("alarm_night") != night:
            ring = now + timedelta(minutes=2)
            self.write(DP_ALARM1, alarm_bytes(self.state["alarm1_saved_b64"], ring.hour, ring.minute), "aurora wake-up")
            self.write(DP_ALARM1_ON, True, "alarm 1 on")
            self.state["alarm"] = {"set_at": time.time()}
            self.state["alarm_night"] = night
        elif a and (mood != "aurora_storm" or time.time() - a["set_at"] > 3600):
            self.write(DP_ALARM1, self.state["alarm1_saved_b64"], "restore alarm 1")
            self.write(DP_ALARM1_ON, False, "alarm 1 off")
            self.state.pop("alarm")

    def on_report(self, dps):
        """Track changes made in the phone app so we never fight them."""
        pl = dps.get(str(DP_PLAYLIST))
        if pl and pl != self.state.get("last_written_playlist"):
            if self.state.get("mood") in (None, "day"):
                self.state["baseline_playlist_b64"] = pl
                log("app changed the playlist during day mood: adopted as the new day playlist")
            else:
                self.state["paused_until"] = time.time() + self.cfg["manual_pause_hours"] * 3600
                log(f"app changed the playlist: pausing for {self.cfg['manual_pause_hours']} h")
            self.state["last_written_playlist"] = pl
        al = dps.get(str(DP_ALARM1))
        if al and not self.state.get("alarm"):
            self.state["alarm1_saved_b64"] = al
        save_json(STATE, self.state)

    def run(self):
        interval = self.cfg["interval_minutes"] * 60
        next_eval, next_hb = 0, 0
        while True:
            try:
                self.update_claude()  # first, so a restart mid-turn doesn't flash the mood playlist
                if time.time() >= next_eval:
                    self.evaluate()
                    to_boundary = seconds_to_night_boundary(self.cfg)
                    next_eval = time.time() + min(interval, to_boundary or interval)
                if self.dry:
                    time.sleep(5)
                    continue
                if time.time() >= next_hb:
                    self.clock.heartbeat()
                    next_hb = time.time() + 9
                msg = self.clock.receive()
                if msg and msg.get("dps"):
                    self.on_report(msg["dps"])
            except Exception as e:
                log(f"error: {e!r}; retrying in 60 s")
                time.sleep(60)
                next_eval = min(next_eval, time.time())


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("status", "once", "run", "preview"):
        sys.exit(__doc__)
    cfg = load_json(os.path.join(ROOT, "config.local.json"), None)
    if not cfg:
        sys.exit("missing config.local.json (copy config.example.json and set lat/lon)")
    if args[0] == "status":
        sky = read_sky(cfg)
        print(json.dumps(sky, indent=1), "\nmood:", decide(sky, cfg))
        return
    if args[0] == "preview":
        c = Controller(cfg, dry_run=False, persist=False)
        mood, secs = args[1], int(args[2]) if len(args) > 2 else 60
        pl, bl = c.playlist_for(mood)
        c.write(DP_PLAYLIST, pl, f"preview {mood}")
        c.write(DP_BACKLIGHT, bl, f"backlight {bl}")
        time.sleep(secs)
        back = c.state.get("mood") or "day"
        pl, bl = c.playlist_for(back)
        c.write(DP_PLAYLIST, pl, f"restore {back}")
        c.write(DP_BACKLIGHT, bl, f"backlight {bl}")
        c.state["shown_playlist"], c.state["shown_backlight"] = pl, bl
        save_json(STATE, c.state)
        return
    c = Controller(cfg, dry_run="--dry-run" in args, persist=args[0] == "run")
    c.evaluate() if args[0] == "once" else c.run()


if __name__ == "__main__":
    main()
