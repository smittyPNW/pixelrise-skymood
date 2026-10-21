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


def claude_busy(cfg):
    """True while any Claude Code session is mid-turn.

    The UserPromptSubmit hook writes "busy" and the Stop/SessionEnd hooks write
    "idle" into <busy_dir>/<session_id>. A busy file older than stale_hours is a
    crashed session; idle files older than a day are pruned here."""
    c = cfg.get("claude") or {}
    d = os.path.expanduser(c.get("busy_dir", "~/Library/Application Support/skymood/claude-busy"))
    now, busy = time.time(), False
    try:
        entries = list(os.scandir(d))
    except OSError:
        return False
    for e in entries:
        try:
            age = now - e.stat().st_mtime
            with open(e.path) as f:
                state = f.read(8).strip()
        except OSError:
            continue
        if state == "busy" and age < c.get("stale_hours", 2) * 3600:
            busy = True
        elif age > 86400:
            try:
                os.remove(e.path)
            except OSError:
                pass
    return busy


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
        fc = [r for r in fetch("products/noaa-planetary-k-index-forecast.json") if r.get("observed") == "predicted"]
        tonight = [r["kp"] for r in fc if any(utc(r["time_tag"]) <= t < utc(r["time_tag"]) + timedelta(hours=3) for t in dark)]
        sky["kp_tonight_max"] = max(tonight) if tonight else None
    except Exception as e:
        sky["kp_tonight_max"] = None
        sky["forecast_error"] = str(e)
    try:
        sky["bz"] = fetch("products/summary/solar-wind-mag-field.json")[0].get("bz_gsm")
    except Exception as e:
        sky["bz"] = None
        sky["bz_error"] = str(e)
    try:
        sky["ovation_view"] = ovation_view_prob(fetch("json/ovation_aurora_latest.json")["coordinates"], lat, lon)
    except Exception as e:
        sky["ovation_view"] = None
        sky["ovation_error"] = str(e)
    return sky


def decide(sky, cfg):
    t = cfg["thresholds"]
    dark = sky["sun_alt"] < cfg["dark_sun_alt"]
    kp = sky.get("kp_now") or 0
    view = sky.get("ovation_view") or 0
    bz = sky.get("bz")
    tonight = sky.get("kp_tonight_max") or 0
    if dark and (kp >= t["storm_kp"] or view >= t["storm_view_prob"]):
        return "aurora_storm"
    if dark and (kp >= t["watch_kp"] or view >= t["watch_view_prob"] or tonight >= t["storm_kp"]
                 or (bz is not None and bz <= t["watch_bz"])):
        return "aurora_watch"
    if sky["sun_alt"] < cfg["night_sun_alt"]:
        return "night"
    return "day"


# ----------------------------------------------------------------- clock
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
        self.claude_on, self.claude_last_busy = False, 0.0

    def playlist_for(self, mood):
        if mood == "day":
            return self.state["baseline_playlist_b64"], self.state["baseline_backlight"]
        m = self.cfg["moods"][mood]
        return encode_playlist(m["playlist"]), m["backlight"]

    def target(self):
        """What the clock should show: aurora moods win, then the Claude scene
        (at the current mood's brightness), then the sky mood."""
        mood = self.state.get("mood") or "day"
        if mood.startswith("aurora") or not self.claude_on:
            return self.playlist_for(mood)
        return self.playlist_for("claude")[0], self.playlist_for(mood)[1]

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
        c = self.cfg.get("claude") or {}
        if not c.get("enabled"):
            return
        now = time.time()
        if claude_busy(self.cfg):
            self.claude_last_busy = now
        on = now - self.claude_last_busy < c.get("linger_seconds", 60)
        if self.state.get("mood") == "night" and not c.get("at_night", True):
            on = False
        if on != self.claude_on:
            self.claude_on = on
            self.apply("Claude Code working" if on else "Claude Code idle")
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
        mood = decide(sky, self.cfg)
        cur = self.state.get("mood")
        rank = ["day", "night", "aurora_watch", "aurora_storm"]
        now = time.time()
        if self.state.get("paused_until", 0) > now:
            log(f"sky {json.dumps(sky)} -> {mood} (paused: manual change in the app)")
            return sky, mood
        # escalate at once; step down only after the calmer mood has held a while
        if cur and mood != cur and rank.index(mood) < rank.index(cur):
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
                if time.time() >= next_eval:
                    self.evaluate()
                    next_eval = time.time() + interval
                self.update_claude()
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
