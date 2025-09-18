#!/usr/bin/env python3
import os, json, time, socket, shutil, subprocess, struct, random
from datetime import datetime
from typing import List, Dict, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import psutil

# ---------- CONFIG ----------
OWNCLOUD_LOG = os.environ.get("OWNCLOUD_LOG", "/var/www/owncloud/data/owncloud.log")  # adjust to your path
BACKUP_SUMMARY = os.environ.get("BACKUP_SUMMARY", "/var/log/vps-backup.json")
BACKUP_HISTORY = os.environ.get("BACKUP_HISTORY", "/var/log/vps-backup-history.jsonl")
WIREGUARD_IFACE = os.environ.get("WIREGUARD_IFACE", "wg0")
MAX_LOG_LINES  = int(os.environ.get("MAX_LOG_LINES", "50"))
MC_HOST = os.environ.get("MC_HOST", "127.0.0.1")
MC_PORT = int(os.environ.get("MC_PORT", 19132))
MC_CONTAINER = os.environ.get("MC_CONTAINER", "bedrock")
MC_TIMEOUT = 2.0
# ----------------------------

app = FastAPI(title="VPS Dashboard API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

def fmt_bytes(b: int) -> str:
    for unit in ["B","KB","MB","GB","TB"]:
        if b < 1024: return f"{b:.1f} {unit}"
        b/=1024
    return f"{b:.1f} PB"

def try_cmd(cmd: List[str]) -> str:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=2)
        return out.strip()
    except Exception:
        return ""

def tail_file(path: str, n: int) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 1024
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            lines = data.decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except Exception:
        return []
    
def bedrock_status(host: str, port: int):
    """
    Unconnected RakNet ping (Bedrock). Returns dict with online, motd, version, players, max_players.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(MC_TIMEOUT)

        # Build UNCONNECTED_PING (0x01)
        # 0x01 + 8-byte time + 16-byte magic + 8-byte client GUID
        magic = b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78"
        payload = b"\x01" + struct.pack(">Q", int(time.time()*1000)) + magic + struct.pack(">Q", random.getrandbits(64))
        s.sendto(payload, (host, port))

        data, _ = s.recvfrom(2048)  # expect UNCONNECTED_PONG (0x1C)
        s.close()
        if not data or data[0] != 0x1c:
            return {"online": False}

        # The server ID string comes after magic, null-terminated, format:
        # "MCPE;MOTD;Protocol;Version;Online;Max;ServerID;LevelName;GameMode;GameModeID"
        # Find the magic and split after it
        idx = data.find(magic)
        if idx == -1:
            return {"online": True}

        sid = data[idx+len(magic):].decode("utf-8", errors="ignore").strip("\x00")
        parts = sid.split(";")
        # Defensive parsing
        motd       = parts[1] if len(parts) > 1 else "Minecraft Bedrock"
        version    = parts[3] if len(parts) > 3 else "Bedrock"
        online     = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        max_players= int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

        return {
            "online": True,
            "motd": motd,
            "version": version,
            "players": [],              # Bedrock ping doesn't list names
            "player_count": online,
            "max_players": max_players,
        }
    except Exception:
        return {"online": False}
    
@app.get("/api/minecraft")
def minecraft_info():
    try:
        container = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", MC_CONTAINER],
            text=True
        ).strip()
    except subprocess.CalledProcessError:
        container = "unknown"

    # Use Bedrock ping (not Java Query)
    mc = bedrock_status(MC_HOST, MC_PORT)

    # If ping failed but container runs, still show Online (fallback)
    if not mc.get("online") and container == "running":
        mc["online"] = True
        mc.setdefault("motd", "Minecraft Bedrock")
        mc.setdefault("version", "Bedrock")
        mc.setdefault("player_count", 0)
        mc.setdefault("max_players", 0)
        mc.setdefault("players", [])

    return {"container": container, "server": mc}

@app.get("/api/metrics")
def metrics():
    cpu = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    ld1, ld5, ld15 = os.getloadavg() if hasattr(os, "getloadavg") else (0,0,0)
    uptime = int(time.time() - psutil.boot_time())
    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype and not part.mountpoint.startswith("/snap"):
            try:
                u = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "mount": part.mountpoint,
                    "total": u.total,
                    "used": u.used,
                    "free": u.free,
                    "percent": u.percent
                })
            except Exception:
                pass
    net = psutil.net_io_counters()
    host = socket.gethostname()
    return {
        "hostname": host,
        "time": datetime.utcnow().isoformat() + "Z",
        "cpu_percent": cpu,
        "load": {"1": ld1, "5": ld5, "15": ld15},
        "memory": {
            "total": vm.total, "available": vm.available,
            "used": vm.used, "percent": vm.percent
        },
        "uptime_seconds": uptime,
        "disks": disks,
        "network": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv}
    }

@app.get("/api/vpn")
def vpn_status():
    """
    Return:
    {
      "type":"wireguard",
      "iface":"wg0",
      "running": true,
      "interface": {"public_key":"...", "listen_port":51820},
      "peers":[
        {
          "peer":"<pubkey>",
          "endpoint":"1.2.3.4:51820",
          "latest_handshake": 1690000000,   # epoch seconds
          "handshake_age_sec": 42,
          "transfer_rx": 1234567,           # bytes
          "transfer_tx": 7654321,           # bytes
          "allowed_ips":"10.0.0.2/32",
          "persistent_keepalive": 25
        }
      ]
    }
    """
    iface = WIREGUARD_IFACE
    # need sudo; make sure sudoers allows: www-data NOPASSWD: /usr/bin/wg show wg0 dump
    raw = try_cmd(["sudo", "/usr/bin/wg", "show", iface, "dump"])
    if not raw:
        # fallback: is unit active?
        if shutil.which("systemctl"):
            unit = f"wg-quick@{iface}.service"
            state = try_cmd(["systemctl","is-active",unit])
            return {"type":"wireguard","iface":iface,"running": state=="active","peers":[]}
        return {"type":"unknown","running":False}

    # wg dump: first line is interface; next lines are peers
    # Interface format: private_key \t public_key \t listen_port \t fwmark
    # Peer format: public_key \t preshared_key \t endpoint \t allowed_ips \t latest_handshake \t rx \t tx \t persistent_keepalive \t reserved
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines: 
        return {"type":"wireguard","iface":iface,"running":False,"peers":[]}

    parts = lines[0].split('\t')
    iface_info = {"public_key": parts[1] if len(parts) > 1 else "", "listen_port": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None}

    peers=[]
    now=int(time.time())
    for l in lines[1:]:
        p=l.split('\t')
        try:
            pk      = p[0]
            endpoint= p[2] if p[2] != "(none)" else ""
            allowed = p[3] if p[3] != "(none)" else ""
            hs      = int(p[4]) if p[4].isdigit() else 0
            rx      = int(p[5]) if p[5].isdigit() else 0
            tx      = int(p[6]) if p[6].isdigit() else 0
            keep    = int(p[7]) if p[7].isdigit() else 0
            peers.append({
                "peer": pk,
                "endpoint": endpoint,
                "allowed_ips": allowed,
                "latest_handshake": hs,
                "handshake_age_sec": (now - hs) if hs else None,
                "transfer_rx": rx,
                "transfer_tx": tx,
                "persistent_keepalive": keep or None
            })
        except Exception:
            continue

    return {"type":"wireguard","iface":iface,"running": True, "interface": iface_info, "peers": peers}


@app.get("/api/owncloud/recent")
def owncloud_recent():
    lines = tail_file(OWNCLOUD_LOG, MAX_LOG_LINES)
    # Attempt to parse JSON lines (ownCloud logs are JSON by default)
    events = []
    for L in lines:
        try:
            obj = json.loads(L)
            events.append({
                "time": obj.get("time") or obj.get("datetime") or "",
                "level": obj.get("level",""),
                "app": obj.get("app",""),
                "message": obj.get("message") or obj.get("msg") or obj.get("reqId","")
            })
        except Exception:
            # fallback raw line
            events.append({"time":"","level":"","app":"raw","message":L[-200:]})
    return {"events": events[-5:]}

@app.get("/api/backups/summary")
def backups_summary():
    latest, history = {}, []
    try:
        with open(BACKUP_SUMMARY,"r") as f:
            latest = json.load(f)
    except Exception:
        latest = {"status":"unknown"}
    try:
        lines = tail_file(BACKUP_HISTORY, MAX_LOG_LINES)
        for L in lines:
            try:
                history.append(json.loads(L))
            except Exception:
                history.append({"raw": L})
    except Exception:
        pass
    return {"latest": latest, "recent": history[-5:]}
