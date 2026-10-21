#!/usr/bin/env python3
"""Listen (read-only) to a device's datapoint reports from Tuya's cloud.

  .venv/bin/python -I tools/watch_cloud.py NAME_OR_ID

Uses the session from get_keys.py and the SDK's message queue. Change things in
the app (add an animation to the playlist, change a colour, ...) and every DP
the clock reports back is printed and appended in full to
scans/cloud-reports-<id>.jsonl. That is how we learn which DP carries
pixel/animation data and its byte format. Ctrl-C to stop.
"""
import json
import os
import sys
import time

from tuya_sharing import Manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from get_keys import CLIENT_ID, ROOT, SESSION, Saver  # noqa: E402


def main():
    want = sys.argv[1].lower()
    session = json.load(open(SESSION))
    mgr = Manager(CLIENT_ID, session["user_code"], session["terminal_id"],
                  session["endpoint"], session["token_info"], Saver(session))
    mgr.update_device_cache()
    dev = next((d for d in mgr.device_map.values() if want in (d.id.lower(), (d.name or "").lower())), None)
    if not dev:
        sys.exit("not found; names: " + ", ".join(repr(d.name) for d in mgr.device_map.values()))
    names = {int(k): v.get("status_code") for k, v in (dev.local_strategy or {}).items()}
    log = os.path.join(ROOT, "scans", f"cloud-reports-{dev.id}.jsonl")

    def on_message(msg):
        data = msg.get("data", {})
        if data.get("devId") != dev.id and data.get("bizData", {}).get("devId") != dev.id:
            return
        with open(log, "a") as f:
            f.write(json.dumps({"rx": time.time(), "msg": msg}) + "\n")
        for item in data.get("status", []):
            v = item.get("value")
            s = v if not isinstance(v, str) or len(v) <= 80 else f"{v[:80]}... ({len(v)} chars)"
            print(f"{time.strftime('%H:%M:%S')} dp {item.get('dpId', item.get('code'))} "
                  f"({names.get(item.get('dpId'), '?')}) = {s}", flush=True)

    mgr.refresh_mq()
    mgr.mq.add_message_listener(on_message)
    print(f"watching {dev.name} ({dev.id}); logging to {log}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mgr.mq.stop()


if __name__ == "__main__":
    main()
