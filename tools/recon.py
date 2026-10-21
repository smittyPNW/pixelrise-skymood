#!/usr/bin/env python3
"""Read-only LAN recon for the PixelRise clock project.

Nothing here writes to a device: TCP connect-and-close port checks, one
SSDP M-SEARCH discovery query, a passive listen on the Tuya discovery
ports, and plain HTTP GETs of "/" on open web ports.

usage: python3 -I recon.py ports HOST [HOST ...]
       python3 -I recon.py listen SECONDS
       python3 -I recon.py ssdp SECONDS
       python3 -I recon.py watch HOST [HOST ...]
       python3 -I recon.py http HOST PORT
"""
import asyncio
import json
import socket
import struct
import sys
import time


async def _probe(host, port, sem, timeout):
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except Exception:
            return None
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return port


async def scan(host, ports, concurrency=48, timeout=1.0):
    sem = asyncio.Semaphore(concurrency)
    hits = await asyncio.gather(*(_probe(host, p, sem, timeout) for p in ports))
    return sorted(p for p in hits if p)


def cmd_ports(hosts):
    for h in hosts:
        t = time.time()
        open_ports = asyncio.run(scan(h, range(1, 65536)))
        print(json.dumps({"host": h, "open_tcp": open_ports, "secs": round(time.time() - t, 1)}), flush=True)


def cmd_listen(seconds):
    """Passively record UDP broadcasts on Tuya's discovery ports (6666 plain, 6667 AES)."""
    socks = []
    for port in (6666, 6667, 7000):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", port))
        s.setblocking(False)
        socks.append((port, s))
    seen = {}
    end = time.time() + seconds
    while time.time() < end:
        for port, s in socks:
            try:
                data, addr = s.recvfrom(4096)
            except BlockingIOError:
                continue
            key = (addr[0], port)
            if key not in seen:
                seen[key] = data
                print(json.dumps({"from": addr[0], "port": port, "len": len(data), "hex": data.hex()}), flush=True)
        time.sleep(0.05)
    print(json.dumps({"done": True, "senders": sorted({k[0] for k in seen})}))


def cmd_ssdp(seconds):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n').encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
    s.settimeout(0.5)
    s.sendto(msg, ("239.255.255.250", 1900))
    end = time.time() + seconds
    found = {}
    while time.time() < end:
        try:
            data, addr = s.recvfrom(8192)
        except socket.timeout:
            continue
        lines = data.decode(errors="replace").split("\r\n")
        hdr = {l.split(":", 1)[0].upper(): l.split(":", 1)[1].strip() for l in lines if ":" in l}
        found.setdefault(addr[0], set()).add((hdr.get("SERVER", ""), hdr.get("LOCATION", ""), hdr.get("ST", "")))
    for ip, rows in sorted(found.items()):
        for server, loc, st in sorted(rows):
            print(json.dumps({"ip": ip, "server": server, "location": loc, "st": st}))


def cmd_watch(hosts):
    """Ping each host every ~2 s and print only up/down transitions (for the unplug test)."""
    import subprocess
    state = {}
    while True:
        for h in hosts:
            up = subprocess.call(["ping", "-c1", "-W", "1500", h],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
            if state.get(h) != up:
                state[h] = up
                print(f"{time.strftime('%H:%M:%S')} {h} {'UP' if up else 'DOWN'}", flush=True)
        time.sleep(2)


def cmd_http(host, port):
    s = socket.create_connection((host, int(port)), timeout=3)
    s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
    s.settimeout(3)
    chunks = []
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            chunks.append(c)
            if sum(map(len, chunks)) > 65536:
                break
    except socket.timeout:
        pass
    s.close()
    sys.stdout.write(b"".join(chunks).decode(errors="replace"))


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    {"ports": lambda: cmd_ports(rest),
     "listen": lambda: cmd_listen(float(rest[0])),
     "ssdp": lambda: cmd_ssdp(float(rest[0])),
     "watch": lambda: cmd_watch(rest),
     "http": lambda: cmd_http(*rest)}[cmd]()
