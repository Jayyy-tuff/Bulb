#!/usr/bin/env python3
"""
Bulb OS - a personal game cloud for your home network (think UmbrelOS, for games).

  * https://bulb.local is the management side: the first person to open it creates the
    owner account, then invites family and friends. Everyone gets their own library,
    settings and saves.
  * Upload PC games (a zip of the game folder, a portable .exe or an installer), retro ROMs
    (NES -> 3DS) and HTML5 games. Retro and HTML5 games also play right in the browser.
  * The Bulb Android app signs in here, downloads PC games to the phone and runs them
    with Wine. Saves sync back to the cloud.

Standard library only.  Run:  python3 bulb.py
"""
import hashlib
import hmac
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "3.0.0"

HOME = os.path.expanduser("~")
BULB_HOME = os.environ.get("BULB_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("BULB_DATA") or os.path.join(HOME, ".local", "share", "bulb")
GAMES = os.environ.get("BULB_GAMES") or os.path.join(DATA, "games")
WEB = os.path.join(BULB_HOME, "web")
CONFIG_PATH = os.path.join(DATA, "config.json")
USERS_PATH = os.path.join(DATA, "users.json")
LIBRARY_PATH = os.path.join(DATA, "library.json")
SESSIONS_PATH = os.path.join(DATA, "sessions.json")
CERT = os.path.join(DATA, "cert.pem")
KEY = os.path.join(DATA, "key.pem")
LOGS = os.path.join(DATA, "logs")
APPS = os.path.join(DATA, "apps")

HTTPS_PORT = int(os.environ.get("BULB_PORT", "443"))
HTTP_PORT = int(os.environ.get("BULB_HTTP_PORT", "80"))
COOKIE = "bulb_s"
CHUNK = 1024 * 256
APP_PREFIX = "Bulb · "

# ---------------------------------------------------------------------------
# Consoles (EmulatorJS)
# ---------------------------------------------------------------------------
SYSTEMS = {
    "nes":    {"name": "NES / Famicom", "cores": ["fceumm", "nestopia"], "ext": ["nes", "fds", "unf", "unif"]},
    "snes":   {"name": "Super Nintendo", "cores": ["snes9x", "bsnes"], "ext": ["sfc", "smc", "fig", "swc", "bs"]},
    "n64":    {"name": "Nintendo 64", "cores": ["mupen64plus_next", "parallel_n64"], "ext": ["z64", "n64", "v64"]},
    "gb":     {"name": "Game Boy / Color", "cores": ["gambatte"], "ext": ["gb", "gbc", "sgb"]},
    "gba":    {"name": "Game Boy Advance", "cores": ["mgba"], "ext": ["gba"]},
    "nds":    {"name": "Nintendo DS", "cores": ["melonds", "desmume", "desmume2015"], "ext": ["nds", "dsi"]},
    "3ds":    {"name": "Nintendo 3DS", "cores": ["azahar"], "ext": ["3ds", "cci", "cxi", "3dsx", "app"]},
    "vb":     {"name": "Virtual Boy", "cores": ["beetle_vb"], "ext": ["vb", "vboy"]},
    "psx":    {"name": "PlayStation", "cores": ["pcsx_rearmed", "mednafen_psx_hw"], "ext": ["cue", "chd", "pbp", "m3u"]},
    "psp":    {"name": "PSP", "cores": ["ppsspp"], "ext": ["cso"]},
    "segaMD": {"name": "Genesis / Mega Drive", "cores": ["genesis_plus_gx", "picodrive"], "ext": ["md", "gen", "smd"]},
    "segaMS": {"name": "Master System", "cores": ["smsplus", "genesis_plus_gx", "picodrive"], "ext": ["sms"]},
    "segaGG": {"name": "Game Gear", "cores": ["genesis_plus_gx"], "ext": ["gg"]},
}
THREADED_CORES = {"ppsspp", "azahar", "dosbox_pure"}
NIGHTLY_SYSTEMS = {"3ds"}

DEFAULT_SYSTEM_SETTINGS = {
    "core": "", "channel": "", "shader": "", "virtualGamepad": "auto", "leftHanded": False,
    "showFps": False, "ffRatio": "3.0", "rewind": False, "vsync": True, "volume": 0.8,
    "color": "#ffd23f", "startOnLoaded": True, "bios": "", "cloudSaves": True, "extraOptions": {},
}

DEFAULT_CONFIG = {
    "name": "Bulb",
    "signup": "invite",          # invite | open | closed
}

LOCK = threading.RLock()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def ensure_dirs():
    for d in (DATA, GAMES, LOGS, APPS):
        os.makedirs(d, exist_ok=True)
    for d in ("roms", "bios", "html", "saves", "covers", "pc"):
        os.makedirs(os.path.join(GAMES, d), exist_ok=True)
    try:
        os.chmod(DATA, 0o700)
    except OSError:
        pass


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def deep_merge(base, extra):
    out = json.loads(json.dumps(base))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    with LOCK:
        return deep_merge(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}))


def save_config(cfg):
    with LOCK:
        write_json(CONFIG_PATH, cfg)


def load_library():
    with LOCK:
        lib = read_json(LIBRARY_PATH, {"games": {}})
        lib.setdefault("games", {})
        return lib


def save_library(lib):
    with LOCK:
        write_json(LIBRARY_PATH, lib)


def safe_name(name, fallback="file"):
    name = os.path.basename(str(name or "").replace("\\", "/")).replace("\x00", "")
    name = re.sub(r"[^\w .()\[\]+&',!-]", "_", name).strip(" .")
    return name[:180] or fallback


def safe_join(root, *parts):
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, *parts))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("path escapes root")
    return path


def new_id():
    return secrets.token_hex(6)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def guess_system(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for key, s in SYSTEMS.items():
        if ext in s["ext"]:
            return key
    return ""




class HubError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------------------
# Users, sessions, invites
# ---------------------------------------------------------------------------
def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()}


def check_pw(user, pw):
    p = user.get("password") or {}
    if not p:
        return False
    return hmac.compare_digest(hash_password(pw, p["salt"])["hash"], p["hash"])


def load_users():
    with LOCK:
        u = read_json(USERS_PATH, {"users": {}, "invites": {}})
        u.setdefault("users", {})
        u.setdefault("invites", {})
        return u


def save_users(u):
    with LOCK:
        write_json(USERS_PATH, u)


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,24}$")
COLORS = ["#ffd23f", "#3ff0ff", "#ff3fa4", "#4dffa0", "#b05cff", "#ff8a3f", "#3fa4ff"]


def public_user(u):
    return {k: u.get(k) for k in ("id", "username", "display", "role", "color", "canPC", "canUpload",
                                  "disabled", "created", "lastSeen")}


def make_user(username, password, role, display=None):
    users = load_users()
    if not USERNAME_RE.match(username or ""):
        raise HubError("Usernames are 3-24 letters, numbers, dots, dashes or underscores.")
    if any(u["username"].lower() == username.lower() for u in users["users"].values()):
        raise HubError("That username is taken.")
    if len(password or "") < 6:
        raise HubError("Pick a password with at least 6 characters.")
    uid = new_id()
    users["users"][uid] = {
        "id": uid, "username": username, "display": (display or username)[:40], "role": role,
        "password": hash_password(password), "color": COLORS[len(users["users"]) % len(COLORS)],
        "canPC": True, "canUpload": True, "disabled": False, "created": time.time(), "lastSeen": time.time(),
        "prefs": {"stream": {}, "systems": {}},
    }
    save_users(users)
    return users["users"][uid]


SESSIONS = {}


def load_sessions():
    global SESSIONS
    now = time.time()
    SESSIONS = {k: v for k, v in read_json(SESSIONS_PATH, {}).items() if isinstance(v, dict) and v.get("exp", 0) > now}


def new_session(uid, device=None, kind="web"):
    tok = secrets.token_urlsafe(32)
    with LOCK:
        SESSIONS[tok] = {"uid": uid, "exp": time.time() + 60 * 60 * 24 * (365 if kind == "app" else 60),
                         "kind": kind, "device": (device or "")[:60], "created": time.time(), "used": time.time()}
        write_json(SESSIONS_PATH, SESSIONS)
    return tok


def session_id(tok):
    return hashlib.sha256(tok.encode()).hexdigest()[:12]


def drop_sessions(tok=None, uid=None):
    with LOCK:
        for k in list(SESSIONS):
            if k == tok or (uid and SESSIONS[k].get("uid") == uid):
                SESSIONS.pop(k, None)
        write_json(SESSIONS_PATH, SESSIONS)


def user_prefs(user):
    p = user.get("prefs") or {}
    return {"systems": p.get("systems") or {}}


def system_settings(user, system):
    return deep_merge(DEFAULT_SYSTEM_SETTINGS, user_prefs(user)["systems"].get(system, {}))


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------
def ensure_cert(force=False):
    if not force and os.path.exists(CERT) and os.path.exists(KEY):
        return True
    ip = local_ip()
    host = socket.gethostname()
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-keyout", KEY,
           "-out", CERT, "-subj", "/CN=bulb.local",
           "-addext", f"subjectAltName=DNS:bulb.local,DNS:{host}.local,DNS:localhost,IP:127.0.0.1,IP:{ip}"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        os.chmod(KEY, 0o600)
        return True
    except Exception as e:
        log("Could not create a certificate (install openssl):", e)
        return False




# ---------------------------------------------------------------------------
# Uploaded PC games (the Bulb app downloads them and runs them with Wine)
# ---------------------------------------------------------------------------
EXE_SKIP = re.compile(r"(unins|setup|install|redist|vcredist|dxsetup|directx|crash|report|launcherhelper|"
                      r"ue4prereq|dotnet|easyanticheat_setup|uploader|helper|update|patch)", re.I)


def pc_game_dir(gid):
    return safe_join(os.path.join(GAMES, "pc"), gid)


def scan_exes(gid):
    root = pc_game_dir(gid)
    found = []
    for base, label in ((os.path.join(root, "files"), "files"), (os.path.join(root, "prefix", "drive_c"), "prefix")):
        for dirpath, dirs, files in os.walk(base):
            rel_dir = os.path.relpath(dirpath, base)
            if label == "prefix" and rel_dir.split(os.sep)[0].lower() in ("windows", "programdata", "users"):
                dirs[:] = []
                continue
            for f in files:
                if f.lower().endswith(".exe"):
                    p = os.path.join(dirpath, f)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        size = 0
                    found.append({"path": os.path.relpath(p, root), "name": f, "size": size,
                                  "likely": not EXE_SKIP.search(f)})
    found.sort(key=lambda e: (not e["likely"], -e["size"]))
    return found


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------
def system_stats():
    stats = {"version": VERSION, "ip": local_ip(), "hostname": socket.gethostname(), "httpsPort": HTTPS_PORT,
             "arch": platform.machine(), "uptime": None, "mem": None, "disk": None, "load": None, "gpu": None}
    try:
        with open("/proc/uptime") as f:
            stats["uptime"] = float(f.read().split()[0])
    except Exception:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0]) * 1024
        stats["mem"] = {"total": info.get("MemTotal"), "free": info.get("MemAvailable", info.get("MemFree"))}
    except Exception:
        pass
    try:
        du = shutil.disk_usage(GAMES)
        stats["disk"] = {"total": du.total, "free": du.free}
    except Exception:
        pass
    try:
        stats["load"] = os.getloadavg()[0]
    except Exception:
        pass
    return stats


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BulbCloud/" + VERSION
    protocol_version = "HTTP/1.1"
    timeout = 600

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(30)
            self.request.do_handshake()
        super().setup()

    def log_message(self, fmt, *args):
        if os.environ.get("BULB_DEBUG"):
            log(self.address_string(), fmt % args)

    # helpers
    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def bearer_token(self):
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def current_user(self):
        tok = self.bearer_token() or self.cookie_token()
        self._token = tok
        s = SESSIONS.get(tok) if tok else None
        if not s or s.get("exp", 0) < time.time():
            return None
        users = load_users()
        u = users["users"].get(s["uid"])
        if not u or u.get("disabled"):
            return None
        if time.time() - (u.get("lastSeen") or 0) > 300:
            u["lastSeen"] = time.time()
            save_users(users)
        if time.time() - s.get("used", 0) > 300:
            with LOCK:
                s["used"] = time.time()
                write_json(SESSIONS_PATH, SESSIONS)
        return u

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            for item in (v if isinstance(v, list) else [v]):
                self.send_header(k, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5 * 1024 * 1024:
            raise HubError("Request too large", 413)
        try:
            return json.loads(self.rfile.read(n) if n else b"{}")
        except ValueError:
            raise HubError("Bad JSON")

    def receive_file(self, dest):
        n = int(self.headers.get("Content-Length") or -1)
        if n < 0:
            raise HubError("Upload needs a Content-Length", 411)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        left = n
        with open(tmp, "wb") as f:
            while left > 0:
                b = self.rfile.read(min(CHUNK, left))
                if not b:
                    raise HubError("Upload interrupted")
                f.write(b)
                left -= len(b)
        os.replace(tmp, dest)
        return n

    def send_file(self, path, extra_headers=None, cache=False):
        if not os.path.isfile(path):
            return self.error("Not found", 404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and size:
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-cache")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(b)

    def host_name(self):
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            host = host[1:host.index("]")]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host or local_ip()

    def session_cookie(self, tok, max_age=60 * 60 * 24 * 60):
        return f"{COOKIE}={tok}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"

    # routing
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(WEB, "index.html"))
            if path in ("/play", "/play.html"):
                hdrs = {}
                if q.get("threads") == "1":
                    hdrs = {"Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "credentialless"}
                return self.send_file(os.path.join(WEB, "play.html"), hdrs)
            if path in ("/manifest.json", "/icon.svg"):
                return self.send_file(os.path.join(WEB, path.lstrip("/")), cache=True)
            if path == "/api/status":
                return self.api_status()
            if path == "/auth/web":
                tok = q.get("t") or ""
                nxt = q.get("next") or "/"
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                s = SESSIONS.get(tok)
                if not s or s.get("kind") != "app" or s.get("exp", 0) < time.time():
                    return self.error("Sign in again in the Bulb app.", 401)
                web = new_session(s["uid"], (s.get("device") or "Bulb app") + " (games)")
                self.send_response(302)
                self.send_header("Location", nxt)
                self.send_header("Set-Cookie", self.session_cookie(web))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/api/auth/") and method == "POST":
                return self.api_auth(path.rsplit("/", 1)[1])
            user = self.current_user()
            if not user:
                return self.error("Sign in first", 401)
            if path.startswith("/files/"):
                return self.serve_files(path, user)
            if path.startswith("/api/"):
                return self.api(method, path, q, user)
            return self.error("Not found", 404)
        except HubError as e:
            return self.error(str(e), e.status)
        except ValueError as e:
            if "escapes root" in str(e):
                return self.error("Invalid file path", 400)
            log("Error on", method, path, repr(e))
            return self.error(f"Server error: {e}", 500)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log("Error on", method, path, repr(e))
            return self.error(f"Server error: {e}", 500)

    def serve_files(self, path, user):
        parts = path.split("/")[2:]
        if len(parts) < 2:
            return self.error("Not found", 404)
        kind = parts[0]
        if kind in ("roms", "html"):
            lib = load_library()
            gid = parts[1] if kind == "html" else None
            if kind == "roms":
                g = next((g for g in lib["games"].values() if g.get("kind") == "rom" and g.get("system") == parts[1]
                          and g.get("file") == "/".join(parts[2:])), None)
            else:
                g = lib["games"].get(gid)
            if not g or not self.can_see(user, g):
                return self.error("Not found", 404)
            return self.send_file(safe_join(os.path.join(GAMES, kind), *parts[1:]), cache=(kind == "roms"))
        if kind == "pc":
            g = load_library()["games"].get(parts[1])
            if not g or g.get("kind") != "pc" or not self.can_see(user, g):
                return self.error("Not found", 404)
            return self.send_file(safe_join(os.path.join(GAMES, "pc", parts[1]), *parts[2:]))
        if kind == "bios":
            return self.send_file(safe_join(os.path.join(GAMES, "bios"), *parts[1:]), cache=True)
        return self.error("Not found", 404)

    # ---------------- auth ----------------
    def api_status(self):
        cfg = load_config()
        users = load_users()
        u = self.current_user()
        return self.send_json({"setup": bool(users["users"]), "signup": cfg["signup"], "name": cfg["name"],
                               "version": VERSION, "user": public_user(u) if u else None})

    def api_auth(self, action):
        cfg = load_config()
        users = load_users()
        b = self.body_json()
        if action == "logout":
            drop_sessions(tok=self.cookie_token())
            return self.send_json({"ok": True}, headers={"Set-Cookie": self.session_cookie("", 0)})
        if action == "setup":
            if users["users"]:
                return self.error("Bulb is already set up. Sign in instead.", 409)
            u = make_user(str(b.get("username", "")).strip(), str(b.get("password", "")), "owner", b.get("display"))
            if b.get("name"):
                cfg["name"] = str(b["name"])[:40]
                save_config(cfg)
            return self.send_json({"ok": True, "user": public_user(u)},
                                  headers={"Set-Cookie": self.session_cookie(new_session(u["id"]))})
        if action == "signup":
            if not users["users"]:
                return self.error("Set up the owner account first.", 409)
            code = str(b.get("invite") or "").strip().upper()
            inv = users["invites"].get(code) if code else None
            if inv and inv.get("expires", 0) < time.time():
                inv = None
            if cfg["signup"] == "closed" and not inv:
                return self.error("Sign-ups are closed. Ask the owner for an invite code.", 403)
            if cfg["signup"] == "invite" and not inv:
                return self.error("You need a valid invite code to join.", 403)
            role = "admin" if inv and inv.get("role") == "admin" else "user"
            u = make_user(str(b.get("username", "")).strip(), str(b.get("password", "")), role, b.get("display"))
            if inv:
                users = load_users()
                inv = users["invites"].get(code)
                if inv:
                    inv["uses"] = int(inv.get("uses", 1)) - 1
                    if inv["uses"] <= 0:
                        users["invites"].pop(code, None)
                    save_users(users)
            return self.send_json({"ok": True, "user": public_user(u)},
                                  headers={"Set-Cookie": self.session_cookie(new_session(u["id"]))})
        if action in ("login", "token"):
            name = str(b.get("username", "")).strip().lower()
            u = next((x for x in users["users"].values() if x["username"].lower() == name), None)
            if not u or not check_pw(u, str(b.get("password", ""))):
                time.sleep(1)
                return self.error("Wrong username or password", 403)
            if u.get("disabled"):
                return self.error("This account is disabled. Ask the owner.", 403)
            if action == "token":  # the Bulb app signs in with a long-lived device token
                tok = new_session(u["id"], device=str(b.get("device") or "Bulb app"), kind="app")
                return self.send_json({"ok": True, "token": tok, "user": public_user(u), "name": cfg["name"]})
            return self.send_json({"ok": True, "user": public_user(u)},
                                  headers={"Set-Cookie": self.session_cookie(new_session(u["id"], self.user_agent()))})
        return self.error("Unknown", 404)

    def user_agent(self):
        ua = self.headers.get("User-Agent") or ""
        for name in ("BulbApp", "Edg", "Chrome", "Firefox", "Safari"):
            if name in ua:
                os_name = "Android" if "Android" in ua else "Windows" if "Windows" in ua else "iPhone" if "iPhone" in ua \
                    else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else ""
                return f"{'Bulb app' if name == 'BulbApp' else name} on {os_name}".strip()
        return "Browser"

    # ---------------- permissions ----------------
    @staticmethod
    def is_admin(user):
        return user.get("role") in ("owner", "admin")

    def need_admin(self, user):
        if not self.is_admin(user):
            raise HubError("Only the owner or an admin can do that.", 403)

    def need_pc(self, user):
        if not user.get("canPC", True):
            raise HubError("Your account can't play PC games. Ask the owner.", 403)

    def need_upload(self, user):
        if not user.get("canUpload", True):
            raise HubError("Your account can't upload games. Ask the owner.", 403)

    def can_see(self, user, g):
        if g.get("kind") == "pc":
            return user.get("canPC", True)
        return g.get("owner") == user["id"] or g.get("shared")

    def can_edit(self, user, g):
        return g.get("owner") == user["id"] or self.is_admin(user)

    # ---------------- API ----------------
    def api(self, method, path, q, user):
        cfg = load_config()
        seg = path.strip("/").split("/")[1:]
        top = seg[0] if seg else ""

        if top == "me":
            return self.api_me(method, seg[1:], user)
        if top == "admin":
            self.need_admin(user)
            return self.api_admin(method, seg[1:], q, user, cfg)
        if top == "system":
            s = system_stats()
            s["name"] = cfg["name"]
            return self.send_json(s)
        if top == "systems":
            if len(seg) == 1:
                return self.send_json({k: {**v, "settings": system_settings(user, k), "default_core": v["cores"][0],
                                           "bios": self.list_bios(k)} for k, v in SYSTEMS.items()})
            sysid = seg[1]
            if sysid not in SYSTEMS:
                return self.error("Unknown system", 404)
            users = load_users()
            u = users["users"][user["id"]]
            if method in ("PUT", "POST", "PATCH"):
                b = self.body_json()
                cur = u.setdefault("prefs", {}).setdefault("systems", {}).setdefault(sysid, {})
                cur.update({k: v for k, v in b.items() if k in DEFAULT_SYSTEM_SETTINGS})
                save_users(users)
            return self.send_json(system_settings(u, sysid))
        if top == "library":
            lib = load_library()
            games = [self.game_out(g, user) for g in lib["games"].values() if self.can_see(user, g)]
            games.sort(key=lambda g: (-(g.get("myLastPlayed") or 0), g["title"].lower()))
            return self.send_json({"games": games, "systems": {k: v["name"] for k, v in SYSTEMS.items()}})
        if top == "games" and len(seg) >= 2:
            return self.api_game(method, seg[1], seg[2:], q, user, cfg)
        if top == "upload" and method in ("PUT", "POST") and len(seg) == 2:
            self.need_upload(user)
            return self.api_upload(seg[1], q, user)
        if top == "saves" and len(seg) == 3:
            gid, kind = seg[1], seg[2]
            if kind not in ("state", "sram", "pc") or not re.fullmatch(r"[0-9a-f]{12}", gid):
                return self.error("Bad save path")
            p = os.path.join(GAMES, "saves", user["id"], gid, kind + ".bin")
            if method == "GET":
                return self.send_file(p)
            if method in ("PUT", "POST"):
                size = self.receive_file(p)
                return self.send_json({"ok": True, "size": size})
            if method == "DELETE":
                if os.path.exists(p):
                    os.remove(p)
                return self.send_json({"ok": True})
        return self.error("Unknown endpoint", 404)

    def api_me(self, method, rest, user):
        users = load_users()
        u = users["users"][user["id"]]
        if not rest and method in ("PATCH", "PUT", "POST"):
            b = self.body_json()
            if b.get("display"):
                u["display"] = str(b["display"])[:40]
            if b.get("color") in COLORS or re.fullmatch(r"#[0-9a-fA-F]{6}", str(b.get("color") or "")):
                u["color"] = b["color"]
            save_users(users)
        if rest[:1] == ["devices"]:
            if method == "DELETE" and len(rest) == 2:
                with LOCK:
                    for t in [t for t, v in SESSIONS.items() if v.get("uid") == u["id"] and session_id(t) == rest[1]]:
                        SESSIONS.pop(t, None)
                    write_json(SESSIONS_PATH, SESSIONS)
                return self.send_json({"ok": True})
            mine = [{"id": session_id(t), "device": v.get("device") or "Browser", "kind": v.get("kind", "web"),
                     "created": v.get("created"), "used": v.get("used"), "current": t == self._token}
                    for t, v in SESSIONS.items() if v.get("uid") == u["id"] and v.get("exp", 0) > time.time()]
            mine.sort(key=lambda d: -(d["used"] or 0))
            return self.send_json({"devices": mine})
        if rest == ["password"] and method == "POST":
            b = self.body_json()
            if not check_pw(u, str(b.get("current") or "")):
                return self.error("Current password is wrong", 403)
            if len(str(b.get("new") or "")) < 6:
                return self.error("New password needs at least 6 characters")
            u["password"] = hash_password(str(b["new"]))
            save_users(users)
            drop_sessions(uid=u["id"])
            return self.send_json({"ok": True}, headers={"Set-Cookie": self.session_cookie(new_session(u["id"]))})
        return self.send_json(public_user(u))

    def api_admin(self, method, rest, q, user, cfg):
        users = load_users()
        what = rest[0] if rest else ""
        if what == "users":
            if len(rest) == 1:
                return self.send_json({"users": [public_user(u) for u in users["users"].values()]})
            uid = rest[1]
            u = users["users"].get(uid)
            if not u:
                return self.error("No such user", 404)
            if u["role"] == "owner" and user["role"] != "owner":
                return self.error("Only the owner can change the owner account.", 403)
            if method == "DELETE":
                if u["role"] == "owner":
                    return self.error("The owner account can't be deleted.")
                users["users"].pop(uid)
                save_users(users)
                drop_sessions(uid=uid)
                shutil.rmtree(os.path.join(GAMES, "saves", uid), ignore_errors=True)
                lib = load_library()
                for g in lib["games"].values():
                    if g.get("owner") == uid:
                        g["owner"] = user["id"]  # keep their uploads; hand them to the admin who removed them
                save_library(lib)
                return self.send_json({"ok": True})
            b = self.body_json()
            if "role" in b and b["role"] in ("admin", "user") and u["role"] != "owner":
                u["role"] = b["role"]
            for k in ("canPC", "canUpload", "disabled"):
                if k in b and u["role"] != "owner":
                    u[k] = bool(b[k])
            if b.get("display"):
                u["display"] = str(b["display"])[:40]
            if b.get("password"):
                if len(b["password"]) < 6:
                    return self.error("Passwords need at least 6 characters")
                u["password"] = hash_password(b["password"])
                drop_sessions(uid=uid)
            if u.get("disabled"):
                drop_sessions(uid=uid)
            save_users(users)
            return self.send_json(public_user(u))
        if what == "invites":
            if method == "POST":
                b = self.body_json()
                code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
                users["invites"][code] = {"code": code, "by": user["username"], "created": time.time(),
                                          "expires": time.time() + 86400 * int(b.get("days") or 7),
                                          "uses": max(1, int(b.get("uses") or 1)),
                                          "role": "admin" if b.get("role") == "admin" else "user",
                                          "note": str(b.get("note") or "")[:60]}
                save_users(users)
                return self.send_json(users["invites"][code])
            if method == "DELETE" and len(rest) == 2:
                users["invites"].pop(rest[1], None)
                save_users(users)
                return self.send_json({"ok": True})
            now = time.time()
            return self.send_json({"invites": [i for i in users["invites"].values() if i.get("expires", 0) > now]})
        if what == "settings":
            if method in ("PUT", "POST", "PATCH"):
                b = self.body_json()
                if b.get("signup") in ("invite", "open", "closed"):
                    cfg["signup"] = b["signup"]
                if b.get("name"):
                    cfg["name"] = str(b["name"])[:40]
                save_config(cfg)
            return self.send_json({k: cfg[k] for k in ("name", "signup")})
        if what == "storage":
            lib = load_library()
            kinds = {"pc": 0, "rom": 0, "html": 0}
            per_user = {}
            for g in lib["games"].values():
                kinds[g.get("kind", "pc")] = kinds.get(g.get("kind", "pc"), 0) + (g.get("size") or 0)
                per_user[g.get("owner")] = per_user.get(g.get("owner"), 0) + (g.get("size") or 0)
            names = {k: v["display"] for k, v in users["users"].items()}
            du = shutil.disk_usage(GAMES)
            return self.send_json({"total": du.total, "free": du.free, "kinds": kinds,
                                   "saves": dir_size(os.path.join(GAMES, "saves")),
                                   "users": [{"name": names.get(k, "Removed user"), "size": v}
                                             for k, v in sorted(per_user.items(), key=lambda kv: -kv[1])]})
        if what == "power" and method == "POST":
            if user.get("role") != "owner":
                return self.error("Only the owner can restart or shut down Bulb OS.", 403)
            act = self.body_json().get("action")
            cmd = {"restart": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
                   "shutdown": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]}.get(act)
            if not cmd:
                return self.error("Unknown action")
            threading.Timer(1.5, lambda: subprocess.run(cmd)).start()
            return self.send_json({"ok": True})
        return self.error("Unknown admin endpoint", 404)

    def list_bios(self, system):
        try:
            return sorted(f for f in os.listdir(os.path.join(GAMES, "bios", system)) if not f.endswith(".part"))
        except OSError:
            return []

    def game_out(self, g, user):
        out = {k: v for k, v in g.items() if k not in ("plays",)}
        out["myLastPlayed"] = (g.get("plays") or {}).get(user["id"])
        out["mine"] = g.get("owner") == user["id"]
        out["canEdit"] = self.can_edit(user, g)
        if g.get("kind") == "rom":
            out["overrides"] = (g.get("overrides") or {}).get(user["id"], {})
            sd = os.path.join(GAMES, "saves", user["id"], g["id"])
            out["saves"] = {k: os.path.getmtime(os.path.join(sd, k + ".bin"))
                            for k in ("state", "sram") if os.path.exists(os.path.join(sd, k + ".bin"))}
        return out

    # ---------------- uploads ----------------
    def api_upload(self, kind, q, user):
        name = safe_name(q.get("name"), "upload")
        lib = load_library()
        base_g = {"owner": user["id"], "shared": q.get("shared") == "1", "added": time.time()}
        if kind == "rom":
            system = q.get("system") or guess_system(name)
            if system not in SYSTEMS:
                return self.error("Pick which console this ROM is for.")
            d = os.path.join(GAMES, "roms", system)
            base, ext = os.path.splitext(name)
            dest = os.path.join(d, name)
            i = 2
            while os.path.exists(dest):
                dest = os.path.join(d, f"{base} ({i}){ext}")
                i += 1
            size = self.receive_file(dest)
            gid = new_id()
            title = q.get("title") or re.sub(r"\s*[\(\[].*?[\)\]]", "", base).strip() or base
            g = {**base_g, "id": gid, "kind": "rom", "title": title, "system": system,
                 "file": os.path.basename(dest), "size": size, "overrides": {}}
            lib["games"][gid] = g
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        if kind == "bios":
            self.need_admin(user)
            system = q.get("system")
            if system not in SYSTEMS:
                return self.error("Pick which console this BIOS is for.")
            self.receive_file(os.path.join(GAMES, "bios", system, name))
            return self.send_json({"ok": True, "bios": self.list_bios(system)})
        if kind == "html":
            gid = new_id()
            root = os.path.join(GAMES, "html", gid)
            os.makedirs(root, exist_ok=True)
            low = name.lower()
            try:
                if low.endswith((".html", ".htm")):
                    self.receive_file(os.path.join(root, "index.html"))
                    entry = "index.html"
                elif low.endswith(".zip"):
                    zp = os.path.join(root, "upload.zip")
                    self.receive_file(zp)
                    try:
                        self.extract_zip(zp, root)
                    finally:
                        if os.path.exists(zp):
                            os.remove(zp)
                    entry = self.find_html(root)
                    if not entry:
                        raise HubError("That zip has no .html file in it.")
                else:
                    raise HubError("Upload a single .html file or a .zip of the game folder.")
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
            g = {**base_g, "id": gid, "kind": "html", "title": q.get("title") or os.path.splitext(name)[0],
                 "entry": entry, "size": dir_size(root), "orientation": q.get("orientation") or "any"}
            lib["games"][gid] = g
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        if kind == "pc":
            self.need_pc(user)
            gid = new_id()
            root = os.path.join(GAMES, "pc", gid)
            files = os.path.join(root, "files")
            os.makedirs(files, exist_ok=True)
            low = name.lower()
            g = {**base_g, "id": gid, "kind": "pc", "shared": True, "title": q.get("title") or os.path.splitext(name)[0],
                 "exe": "", "installer": "", "args": ""}
            try:
                if low.endswith(".zip"):
                    zp = os.path.join(root, "upload.zip")
                    self.receive_file(zp)
                    try:
                        self.extract_zip(zp, files, limit=200 * 1024 ** 3)
                    finally:
                        if os.path.exists(zp):
                            os.remove(zp)
                elif low.endswith((".exe", ".msi")):
                    self.receive_file(os.path.join(files, name))
                    if re.search(r"setup|install", name, re.I) or low.endswith(".msi"):
                        g["installer"] = os.path.join("files", name)
                else:
                    raise HubError("Upload a .zip of the game folder, or a single .exe (game or installer).")
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
            if not g["installer"]:
                exes = scan_exes(gid)
                g["exe"] = exes[0]["path"] if exes else ""
            g["size"] = dir_size(root)
            lib["games"][gid] = g
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        if kind == "cover":
            gid = q.get("id") or ""
            g = lib["games"].get(gid)
            if not g or not self.can_edit(user, g):
                return self.error("Unknown game", 404)
            self.receive_file(os.path.join(GAMES, "covers", gid + ".img"))
            g["cover"] = int(time.time())
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        return self.error("Unknown upload type", 404)

    def extract_zip(self, zpath, root, limit=8 * 1024 ** 3):
        if not zipfile.is_zipfile(zpath):
            raise HubError("That file isn't a valid .zip.")
        with zipfile.ZipFile(zpath) as z:
            total = 0
            for info in z.infolist():
                if info.is_dir():
                    continue
                total += info.file_size
                if total > limit:
                    raise HubError("That zip is too big when unpacked.")
                dest = safe_join(root, info.filename)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
        # unwrap a single top-level folder
        items = os.listdir(root)
        if len(items) == 1 and os.path.isdir(os.path.join(root, items[0])) and root.endswith("files"):
            inner = os.path.join(root, items[0])
            for x in os.listdir(inner):
                shutil.move(os.path.join(inner, x), os.path.join(root, x))
            os.rmdir(inner)

    def find_html(self, root):
        htmls = []
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".html", ".htm")):
                    htmls.append(os.path.relpath(os.path.join(dirpath, f), root))
        if not htmls:
            return None
        htmls.sort(key=lambda p: (p.count(os.sep), os.path.basename(p).lower() not in ("index.html", "index.htm"), p))
        return htmls[0].replace(os.sep, "/")

    # ---------------- games ----------------
    def api_game(self, method, gid, rest, q, user, cfg):
        lib = load_library()
        g = lib["games"].get(gid)
        if not g or not self.can_see(user, g):
            return self.error("Game not found", 404)
        if rest == ["cover"] and method == "GET":
            return self.send_file(os.path.join(GAMES, "covers", gid + ".img"), cache=True)
        if rest == ["play"]:
            g.setdefault("plays", {})[user["id"]] = time.time()
            save_library(lib)
            if g["kind"] == "html":
                return self.send_json({"kind": "html", "url": f"/files/html/{gid}/{urllib.parse.quote(g['entry'])}",
                                       "title": g["title"], "orientation": g.get("orientation", "any")})
            if g["kind"] == "rom":
                return self.send_json(self.play_config(user, g))
            return self.error("PC games start from /api/pc/play")
        if rest == ["manifest"]:
            return self.send_json(self.manifest(g, user))
        if rest == ["exes"]:
            return self.send_json({"exes": scan_exes(gid) if g["kind"] == "pc" else []})
        if rest == ["overrides"] and method in ("PUT", "POST", "PATCH") and g["kind"] == "rom":
            b = self.body_json()
            g.setdefault("overrides", {})[user["id"]] = {k: v for k, v in b.items() if k in DEFAULT_SYSTEM_SETTINGS}
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        if method in ("PATCH", "PUT", "POST"):
            if not self.can_edit(user, g):
                return self.error("Only the person who added it (or an admin) can change this game.", 403)
            b = self.body_json()
            if b.get("title"):
                g["title"] = str(b["title"])[:120]
            if "shared" in b and g["kind"] != "pc":
                g["shared"] = bool(b["shared"])
            if "orientation" in b and g["kind"] == "html":
                g["orientation"] = b["orientation"]
            if g["kind"] == "pc":
                for k in ("exe", "installer"):
                    if k in b:
                        if b[k]:
                            safe_join(pc_game_dir(gid), b[k])
                        g[k] = b[k] or ""
                if "args" in b:
                    g["args"] = str(b["args"])[:300]
                g["updated"] = time.time()
            if "system" in b and g["kind"] == "rom" and b["system"] in SYSTEMS and b["system"] != g["system"]:
                src = os.path.join(GAMES, "roms", g["system"], g["file"])
                dd = os.path.join(GAMES, "roms", b["system"])
                os.makedirs(dd, exist_ok=True)
                if os.path.exists(src):
                    shutil.move(src, os.path.join(dd, g["file"]))
                g["system"] = b["system"]
                for ov in (g.get("overrides") or {}).values():
                    ov.pop("core", None)
            save_library(lib)
            return self.send_json(self.game_out(g, user))
        if method == "DELETE":
            if not self.can_edit(user, g):
                return self.error("Only the person who added it (or an admin) can delete it.", 403)
            if g["kind"] == "rom":
                p = os.path.join(GAMES, "roms", g["system"], g["file"])
                if os.path.exists(p):
                    os.remove(p)
            elif g["kind"] == "html":
                shutil.rmtree(os.path.join(GAMES, "html", gid), ignore_errors=True)
            elif g["kind"] == "pc":
                shutil.rmtree(pc_game_dir(gid), ignore_errors=True)
            for d in os.listdir(os.path.join(GAMES, "saves")):
                shutil.rmtree(os.path.join(GAMES, "saves", d, gid), ignore_errors=True)
            cover = os.path.join(GAMES, "covers", gid + ".img")
            if os.path.exists(cover):
                os.remove(cover)
            del lib["games"][gid]
            save_library(lib)
            return self.send_json({"ok": True})
        return self.send_json(self.game_out(g, user))

    def manifest(self, g, user):
        """Everything the Bulb app needs to download a game and run it on the phone."""
        if g["kind"] == "pc":
            root = pc_game_dir(g["id"])
            files = []
            for dirpath, _, names in os.walk(os.path.join(root, "files")):
                for n in names:
                    if n.endswith(".part"):
                        continue
                    full = os.path.join(dirpath, n)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    files.append({"path": rel, "size": os.path.getsize(full),
                                  "url": "/files/pc/" + g["id"] + "/" + urllib.parse.quote(rel)})
            files.sort(key=lambda f: f["path"])
            sd = os.path.join(GAMES, "saves", user["id"], g["id"])
            return {"id": g["id"], "kind": "pc", "title": g["title"], "version": int(g.get("updated") or g.get("added") or 0),
                    "exe": g.get("exe", ""), "installer": g.get("installer", ""), "args": g.get("args", ""),
                    "cover": f"/api/games/{g['id']}/cover?v={g['cover']}" if g.get("cover") else "",
                    "files": files, "total": sum(f["size"] for f in files),
                    "save": {"exists": os.path.exists(os.path.join(sd, "pc.bin")), "url": f"/api/saves/{g['id']}/pc"}}
        if g["kind"] == "rom":
            return {"id": g["id"], "kind": "rom", "title": g["title"], "system": g["system"],
                    "play": f"/play?id={g['id']}", "files": [{"path": g["file"], "size": g.get("size", 0),
                                                              "url": f"/files/roms/{g['system']}/{urllib.parse.quote(g['file'])}"}]}
        return {"id": g["id"], "kind": "html", "title": g["title"],
                "play": f"/files/html/{g['id']}/{urllib.parse.quote(g['entry'])}", "files": []}

    def play_config(self, user, g):
        system = g["system"]
        s = deep_merge(system_settings(user, system), (g.get("overrides") or {}).get(user["id"], {}))
        core = s.get("core") or SYSTEMS[system]["cores"][0]
        channel = s.get("channel") or ("nightly" if system in NIGHTLY_SYSTEMS else "stable")
        options = {
            "virtual-gamepad": {"on": "enabled", "off": "disabled"}.get(s.get("virtualGamepad")),
            "virtual-gamepad-left-handed-mode": "enabled" if s.get("leftHanded") else "disabled",
            "fps": "show" if s.get("showFps") else "hide",
            "ff-ratio": str(s.get("ffRatio") or "3.0"),
            "rewindEnabled": "enabled" if s.get("rewind") else "disabled",
            "vsync": "enabled" if s.get("vsync", True) else "disabled",
            "shader": s.get("shader") or None,
        }
        options = {k: v for k, v in options.items() if v is not None}
        if isinstance(s.get("extraOptions"), dict):
            options.update({str(k): str(v) for k, v in s["extraOptions"].items()})
        bios = ""
        if s.get("bios") and os.path.isfile(os.path.join(GAMES, "bios", system, s["bios"])):
            bios = f"/files/bios/{system}/{urllib.parse.quote(s['bios'])}"
        sd = os.path.join(GAMES, "saves", user["id"], g["id"])
        return {"kind": "rom", "id": g["id"], "title": g["title"], "system": system,
                "systemName": SYSTEMS[system]["name"], "core": core, "channel": channel,
                "threads": core in THREADED_CORES,
                "romUrl": f"/files/roms/{system}/{urllib.parse.quote(g['file'])}", "biosUrl": bios,
                "options": options, "volume": float(s.get("volume", 0.8)), "color": s.get("color") or "#ffd23f",
                "startOnLoaded": bool(s.get("startOnLoaded", True)), "cloudSaves": bool(s.get("cloudSaves", True)),
                "hasState": os.path.exists(os.path.join(sd, "state.bin")),
                "hasSram": os.path.exists(os.path.join(sd, "sram.bin"))}

    # ---------------- PC library ----------------
class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or "bulb.local").split(":")[0]
        port = "" if HTTPS_PORT == 443 else f":{HTTPS_PORT}"
        self.send_response(301)
        self.send_header("Location", f"https://{host}{port}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, socket.timeout, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    ensure_dirs()
    load_sessions()
    for ext, t in ((".wasm", "application/wasm"), (".js", "application/javascript"), (".html", "text/html")):
        mimetypes.add_type(t, ext)
    if not ensure_cert():
        sys.exit("Bulb needs openssl to create its HTTPS certificate.")
    httpd = Server(("0.0.0.0", HTTPS_PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    try:
        redirect = Server(("0.0.0.0", HTTP_PORT), RedirectHandler)
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
    except OSError as e:
        log(f"HTTP redirect on port {HTTP_PORT} disabled: {e}")
    def shutdown(*_):
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    port = "" if HTTPS_PORT == 443 else f":{HTTPS_PORT}"
    log(f"Bulb OS {VERSION} is up: https://bulb.local{port}  (or https://{local_ip()}{port})")
    httpd.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset-password":
        users = load_users()
        u = next((x for x in users["users"].values() if x["username"].lower() == sys.argv[2].lower()), None)
        if not u:
            sys.exit("No such user")
        u["password"] = hash_password(sys.argv[3])
        save_users(users)
        print("Password updated for", u["username"])
        sys.exit(0)
    main()
