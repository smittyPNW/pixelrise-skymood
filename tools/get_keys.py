#!/usr/bin/env python3
"""Fetch local keys + datapoint specs for every device in a Smart Life account.

Read-only cloud query through Tuya's device-sharing SDK (the same QR login
Home Assistant's Tuya integration uses; no Tuya developer account needed).
Nothing is sent to any device.

  .venv/bin/python -I tools/get_keys.py USER_CODE
      USER_CODE: Smart Life app -> Me -> gear (Settings) -> Account and Security -> User Code

Writes (git-ignored, mode 600):
  secrets/session.json   login session, reused on the next run
  secrets/devices.json   every device incl. local_key
and the non-secret part (no keys) to scans/devices-spec.json.
"""
import json
import os
import sys
import time
from types import SimpleNamespace

from tuya_sharing import LoginControl, Manager, SharingTokenListener

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"  # Home Assistant's public app registration
SCHEMA = "haauthorize"
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = os.path.join(ROOT, "secrets")
SESSION = os.path.join(SECRETS, "session.json")


def write_private(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


class Saver(SharingTokenListener):
    def __init__(self, session):
        self.session = session

    def update_token(self, token_info):
        self.session["token_info"] = {k: token_info.get(k) for k in TOKEN_FIELDS}
        write_private(SESSION, self.session)


def login(user_code):
    resp = LoginControl().qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        sys.exit(f"QR login refused [{resp.get('code')}]: {resp.get('msg')}")
    token = resp["result"]["qrcode"]
    import qrcode
    png = os.path.join(SECRETS, "login-qr.png")
    os.makedirs(SECRETS, exist_ok=True)
    qrcode.make(f"tuyaSmart--qrLogin?token={token}").save(png)
    os.system(f"open '{png}'")
    print(f"Scan {png} in Smart Life (+ -> Scan), then tap Confirm login.", flush=True)
    for _ in range(90):
        try:
            ok, info = LoginControl().login_result(token, CLIENT_ID, user_code)
        except Exception:
            ok = False
        if ok:
            session = {"user_code": user_code, "terminal_id": info["terminal_id"],
                       "endpoint": info.get("endpoint") or info.get("end_point"),
                       "token_info": {k: info.get(k) for k in TOKEN_FIELDS}}
            write_private(SESSION, session)
            os.remove(png)
            return session
        time.sleep(2)
    sys.exit("Login timed out; re-run and scan promptly.")


def plain(v):
    if isinstance(v, SimpleNamespace):
        v = vars(v)
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def main():
    user_code = sys.argv[1] if len(sys.argv) > 1 else None
    session = None
    if os.path.isfile(SESSION):
        session = json.load(open(SESSION))
    if not session or (user_code and session.get("user_code") != user_code):
        if not user_code:
            sys.exit(__doc__)
        session = login(user_code)
    mgr = Manager(CLIENT_ID, session["user_code"], session["terminal_id"],
                  session["endpoint"], session["token_info"], Saver(session))
    mgr.update_device_cache()
    devices = [plain(vars(d)) for d in mgr.device_map.values()]
    write_private(os.path.join(SECRETS, "devices.json"), devices)
    # cloud "ip" is the home's public WAN address, not the LAN one
    public = [{k: v for k, v in d.items() if k not in ("local_key", "uuid", "owner_id", "uid", "lat", "lon", "asset_id", "ip")}
              for d in devices]
    with open(os.path.join(ROOT, "scans", "devices-spec.json"), "w") as f:
        json.dump(public, f, indent=2, sort_keys=True)
    for d in devices:
        print(f"{d.get('name')!r:32} ip={d.get('ip')} id={d.get('id')} pid={d.get('product_id')} "
              f"cat={d.get('category')} online={d.get('online')} dps={len(d.get('local_strategy') or {})}")


if __name__ == "__main__":
    main()
