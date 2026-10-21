#!/usr/bin/env python3
"""Read-only local status dump of a Tuya device (no writes, no control commands).

  .venv/bin/python -I tools/dump_dps.py NAME_OR_ID [IP ...]

Looks the device up in secrets/devices.json (from get_keys.py), then asks each
candidate IP for its status over TCP 6668 with that key, trying protocol 3.5,
3.4 and 3.3. A wrong IP or version just fails to decrypt and is skipped.
Pass the candidate IPs (devices answering on TCP 6668).
Saves the result to scans/dps-<id>.json (values only, no key).
"""
import json
import os
import sys
import time

import tinytuya

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = []  # pass the IPs of your Tuya devices (find them with tools/recon.py)


def main():
    want = sys.argv[1].lower()
    ips = sys.argv[2:] or CANDIDATES
    if not ips:
        sys.exit("give one or more IPs: dump_dps.py NAME IP [IP ...]")
    devices = json.load(open(os.path.join(ROOT, "secrets", "devices.json")))
    dev = next((d for d in devices if want in (d["id"].lower(), (d.get("name") or "").lower())), None)
    if not dev:
        sys.exit("not found; names: " + ", ".join(repr(d.get("name")) for d in devices))
    for ip in ips:
        for ver in (3.5, 3.4, 3.3):
            d = tinytuya.Device(dev["id"], ip, dev["local_key"], version=ver, connection_timeout=4)
            d.set_socketRetryLimit(1)
            st = d.status()
            d.close()
            if st and "dps" in st:
                out = {"name": dev.get("name"), "id": dev["id"], "lan_ip": ip, "version": ver,
                       "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "dps": st["dps"],
                       "local_strategy": dev.get("local_strategy")}
                path = os.path.join(ROOT, "scans", f"dps-{dev['id']}.json")
                with open(path, "w") as f:
                    json.dump(out, f, indent=2, sort_keys=True)
                print(json.dumps(out["dps"], indent=1))
                print(f"-> {dev.get('name')} is {ip}, protocol {ver}; saved {path}")
                return
    sys.exit(f"no answer from {ips} with this key (wrong IP, or the app holds the one local connection)")


if __name__ == "__main__":
    main()
