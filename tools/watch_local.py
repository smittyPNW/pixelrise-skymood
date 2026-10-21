#!/usr/bin/env python3
"""Listen (read-only) to everything the clock reports over its local connection.

  .venv/bin/python -I tools/watch_local.py NAME_OR_ID IP [VERSION]

Holds one persistent tinytuya connection, sends only heartbeats, and prints
every DP update the clock pushes (including raw DPs that never show up in a
status query) while you change things in the app. Full payloads are appended
to scans/local-reports-<id>.jsonl. Ctrl-C to stop.
"""
import json
import os
import sys
import time

import tinytuya

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    want, ip = sys.argv[1].lower(), sys.argv[2]
    ver = float(sys.argv[3]) if len(sys.argv) > 3 else 3.5
    devices = json.load(open(os.path.join(ROOT, "secrets", "devices.json")))
    dev = next(d for d in devices if want in (d["id"].lower(), (d.get("name") or "").lower()))
    log = os.path.join(ROOT, "scans", f"local-reports-{dev['id']}.jsonl")
    d = tinytuya.Device(dev["id"], ip, dev["local_key"], version=ver, persist=True)
    d.set_socketPersistent(True)
    print(f"watching {dev.get('name')} at {ip} (v{ver}); logging to {log}", flush=True)
    last_hb = 0
    while True:
        if time.time() - last_hb > 9:
            d.heartbeat(nowait=True)
            last_hb = time.time()
        msg = d.receive()
        if not msg:
            continue
        with open(log, "a") as f:
            f.write(json.dumps({"rx": time.time(), "msg": msg}, default=str) + "\n")
        for k, v in (msg.get("dps") or {}).items():
            s = v if not isinstance(v, str) or len(v) <= 80 else f"{v[:80]}... ({len(v)} chars)"
            print(f"{time.strftime('%H:%M:%S')} dp {k} = {s}", flush=True)
        if "Error" in msg:
            print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


if __name__ == "__main__":
    main()
