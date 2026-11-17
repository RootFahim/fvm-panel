#!/usr/bin/env python3
# ============================================================================
#   FVM PANEL  -  fvm.py
#   (c) 2026 Fahim. All rights reserved. Please keep this notice in place.
# ============================================================================
"""FVM PANEL - real LXD/LXC VPS management panel (single-file: backend + frontend).
FVM Panel © 2026 Fahim

Run:  python fvm.py            start the panel
      python fvm.py init       create database + bootstrap admin, then exit
      python fvm.py passwd USER  reset a user's password from the console
"""
__author__ = "Fahim"
__copyright__ = "(c) 2026 Fahim"

import os, re, sys, json, time, shlex, hashlib, secrets, sqlite3, logging, threading, subprocess
import base64, fcntl, platform, pty, select, signal, struct, termios
import urllib.error, urllib.parse, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import psutil
from dotenv import load_dotenv
from flask import Flask, Response, g, has_request_context, jsonify, redirect, request, session
from markupsafe import escape
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))


def _e(k, d=""): return os.environ.get(k, d).strip()
def _b(k, d="false"): return _e(k, d).lower() in ("1", "true", "yes", "on")
def _i(k, d):
    try: return int(_e(k, str(d)))
    except ValueError: return d
def _p(p): return p if os.path.isabs(p) else os.path.join(BASE, p)


class C:  # all configuration comes from .env
    NAME = _e("PANEL_NAME", "FVM PANEL"); VERSION = _e("PANEL_VERSION", "1.0.0")
    HOST = _e("PANEL_HOST", "0.0.0.0"); PORT = _i("PANEL_PORT", 8080); DEBUG = _b("DEBUG")
    SECRET = _e("SECRET_KEY"); DB = _p(_e("DATABASE_PATH", "fvm_panel.db"))
    LOG_FILE = _e("LOG_FILE", "fvm_panel.log"); LOG_LEVEL = _e("LOG_LEVEL", "INFO").upper()
    THREADS = _i("WAITRESS_THREADS", 32); PROXY = _b("TRUST_PROXY"); COOKIE_SECURE = _b("COOKIE_SECURE")
    SESSION_HOURS = _i("SESSION_HOURS", 12)
    ADMIN_USER = _e("ADMIN_USERNAME", "admin"); ADMIN_EMAIL = _e("ADMIN_EMAIL", "admin@example.com")
    ADMIN_PASS = _e("ADMIN_PASSWORD")
    REG = _b("REGISTRATION_ENABLED", "true")
    MIN_PW = _i("MIN_PASSWORD_LENGTH", 8)
    RESET_TTL = _i("RESET_TOKEN_TTL_MIN", 30)
    RAM = _i("DEFAULT_RAM_MB", 1024); CPU = _i("DEFAULT_CPU", 1); DISK = _i("DEFAULT_DISK_GB", 10)
    MAX_RAM = _i("MAX_RAM_MB", 65536); MAX_CPU = _i("MAX_CPU", 64); MAX_DISK = _i("MAX_DISK_GB", 2000)
    ALLOWED_OS = _e("ALLOWED_OS", "ubuntu:20.04,ubuntu:22.04,ubuntu:24.04,debian:11,debian:12,debian:13,alpine:3.20,rockylinux:9,almalinux:9,fedora:41,archlinux:current")
    POOL = _e("LXD_STORAGE_POOL"); REMOTE = _e("LXD_IMAGE_REMOTE", "images"); UBUNTU_REMOTE = _e("LXD_UBUNTU_REMOTE", "ubuntu")
    CONSOLE = _b("CONSOLE_ENABLED", "true"); CMD_TIMEOUT = _i("CMD_TIMEOUT", 60); CREATE_TIMEOUT = _i("CREATE_TIMEOUT", 900)
    MON_INT = _i("MONITOR_INTERVAL", 10); HIST = _i("HISTORY_POINTS", 90); SSH_KEY = _e("SSH_KEY_PATH")
    TERM_MAX = _i("TERMINAL_MAX_PER_USER", 3); TERM_IDLE = _i("TERMINAL_IDLE_MINUTES", 15) * 60
    CDN = _e("TERMINAL_CDN", "https://cdn.jsdelivr.net").rstrip("/")
    PORT_MIN = _i("VPS_PORT_MIN", 1024); PORT_MAX = _i("VPS_PORT_MAX", 65535)
    N_NAME = _e("DEFAULT_NODE_NAME", "node-1"); N_HOST = _e("DEFAULT_NODE_HOST", "127.0.0.1")
    N_PATH = _e("DEFAULT_NODE_STORAGE_PATH", "/"); N_BRIDGE = _e("DEFAULT_NODE_BRIDGE", "lxdbr0")
    N_MAX_VPS = _i("DEFAULT_NODE_MAX_VPS", 0); N_MAX_RAM = _i("DEFAULT_NODE_MAX_RAM_MB", 0)
    N_MAX_CPU = _i("DEFAULT_NODE_MAX_CPU", 0); N_MAX_DISK = _i("DEFAULT_NODE_MAX_DISK_GB", 0)


_h = [logging.StreamHandler()]
if C.LOG_FILE:
    try: _h.append(RotatingFileHandler(_p(C.LOG_FILE), maxBytes=2_000_000, backupCount=3))
    except OSError: pass
logging.basicConfig(level=getattr(logging, C.LOG_LEVEL, logging.INFO), handlers=_h,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fvm")

def _adopt_old_db():
    """Installs that ran as rf.py kept their data in rf_panel.db: move it to the new name once, so nothing is lost."""
    if os.environ.get("DATABASE_PATH") or os.path.exists(C.DB): return
    old = os.path.join(os.path.dirname(C.DB), "rf_panel.db")
    if not os.path.exists(old): return
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(old + suf): os.rename(old + suf, C.DB + suf)
    log.info("Moved the existing database rf_panel.db to %s", os.path.basename(C.DB))


try: _adopt_old_db()
except OSError as e: log.warning("Could not move the old database: %s", e)

if len(C.SECRET) < 32 or C.SECRET.startswith("CHANGE_ME"):
    C.SECRET = secrets.token_hex(32)
    log.warning("SECRET_KEY is missing/weak: using a temporary key (sessions reset on restart). Set a real one in .env")

OS_LIST = [t.strip() for t in C.ALLOWED_OS.split(",") if re.fullmatch(r"[a-z]+:[A-Za-z0-9._-]+", t.strip())]
OS_NAMES = {"ubuntu": "Ubuntu", "debian": "Debian", "alpine": "Alpine Linux", "rockylinux": "Rocky Linux",
            "almalinux": "AlmaLinux", "fedora": "Fedora", "archlinux": "Arch Linux"}
TRANSIENT = ("creating", "starting", "stopping", "restarting", "deleting", "reinstalling")

# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
 email TEXT UNIQUE NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')), suspended INTEGER NOT NULL DEFAULT 0,
 max_vps INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login TEXT, discord_id TEXT, discord_name TEXT, notif_mute TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS nodes(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, host TEXT NOT NULL,
 ssh_user TEXT NOT NULL DEFAULT 'root', ssh_port INTEGER NOT NULL DEFAULT 22, is_local INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 1, max_vps INTEGER NOT NULL DEFAULT 0, max_ram_mb INTEGER NOT NULL DEFAULT 0,
 max_cpu INTEGER NOT NULL DEFAULT 0, max_disk_gb INTEGER NOT NULL DEFAULT 0,
 lxc_path TEXT NOT NULL DEFAULT '/', bridge TEXT NOT NULL DEFAULT 'lxdbr0', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vps(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
 label TEXT, port INTEGER, hostname TEXT NOT NULL, username TEXT NOT NULL, os TEXT NOT NULL, os_version TEXT NOT NULL,
 ram_mb INTEGER NOT NULL, cpu INTEGER NOT NULL, disk_gb INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'creating', suspended INTEGER NOT NULL DEFAULT 0,
 ip TEXT, error TEXT, started_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 vps_id INTEGER, action TEXT NOT NULL, detail TEXT, ip TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reset_tokens(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS i_vps_user ON vps(user_id);
CREATE INDEX IF NOT EXISTS i_vps_node ON vps(node_id);
CREATE TABLE IF NOT EXISTS sessions(
 sid TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, last_seen TEXT NOT NULL, ip TEXT);
CREATE INDEX IF NOT EXISTS i_sess_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 actor_id INTEGER, actor TEXT, text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'info', level TEXT NOT NULL DEFAULT 'info',
 vps_id INTEGER, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS i_notif_user ON notifications(user_id, id);
CREATE INDEX IF NOT EXISTS i_act_user ON activity(user_id);
"""
DEFAULT_SETTINGS = {"panel_name": C.NAME, "registration_enabled": "1" if C.REG else "0",
                    "session_hours": str(C.SESSION_HOURS), "max_sessions": "10",
                    "logo_text": "FVM", "accent": "#6d5efc", "console_enabled": "1" if C.CONSOLE else "0",
                    "site_description": "Virtual machine management system", "site_icon": "", "panel_title": "",
                    "panel_description": "Sign in to manage your cloud",
                    "discord_auth": "0", "discord_client_id": "", "discord_client_secret": "", "discord_redirect": ""}
_tl = threading.local()


def db():
    c = getattr(_tl, "c", None)
    if c is None:
        c = sqlite3.connect(C.DB, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL")
        _tl.c = c
    return c


def q(sql, a=(), one=False):
    cur = db().execute(sql, a)
    if one:
        r = cur.fetchone(); return dict(r) if r else None
    return [dict(r) for r in cur.fetchall()]


def x(sql, a=()):
    cur = db().execute(sql, a); db().commit(); return cur


def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def later(minutes): return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return r["value"] if r else DEFAULT_SETTINGS.get(k, "")


START = time.time()


def _int_setting(k, lo, hi, dflt):
    try: return min(max(int(setting(k)), lo), hi)
    except (TypeError, ValueError): return dflt


def session_hours(): return _int_setting("session_hours", 1, 720, C.SESSION_HOURS)
def max_sessions(): return _int_setting("max_sessions", 1, 100, 10)


def apply_session_lifetime(): app.permanent_session_lifetime = timedelta(hours=session_hours())


def min_pw(): return C.MIN_PW
def login_limit(): return 10
def console_on(): return setting("console_enabled") == "1"


def init_db():
    db().executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items(): x("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if setting("notif_schema") != "2":      # the first design showed other people's actions to admins and owners: notifications are private now
        x("DELETE FROM notifications WHERE actor_id IS NOT NULL AND actor_id <> user_id")
        x("INSERT INTO settings VALUES('notif_schema','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    for k, old, new in (("panel_name", "RF PANEL", "FVM PANEL"), ("panel_name", "RF Panel", "FVM PANEL"), ("panel_title", "RF PANEL", "FVM PANEL"),
                        ("panel_title", "RF Panel", "FVM PANEL"), ("logo_text", "RF", "FVM")):       # rebrand old defaults, keep custom values
        x("UPDATE settings SET value=? WHERE key=? AND value=?", (new, k, old))
    if "label" not in [r["name"] for r in q("PRAGMA table_info(vps)")]: x("ALTER TABLE vps ADD COLUMN label TEXT")
    if "port" not in [r["name"] for r in q("PRAGMA table_info(vps)")]: x("ALTER TABLE vps ADD COLUMN port INTEGER")
    ucols = [r["name"] for r in q("PRAGMA table_info(users)")]
    for col in ("discord_id", "discord_name"):
        if col not in ucols: x(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    if "notif_mute" not in ucols: x("ALTER TABLE users ADD COLUMN notif_mute TEXT NOT NULL DEFAULT ''")
    x("CREATE UNIQUE INDEX IF NOT EXISTS i_user_discord ON users(discord_id)")
    x("UPDATE vps SET label=name WHERE label IS NULL")
    x("UPDATE nodes SET bridge='lxdbr0' WHERE bridge='lxcbr0'")
    if not q("SELECT 1 FROM nodes LIMIT 1", one=True):
        x("INSERT INTO nodes(name,host,is_local,max_vps,max_ram_mb,max_cpu,max_disk_gb,lxc_path,bridge,created_at)"
          " VALUES(?,?,1,?,?,?,?,?,?,?)", (C.N_NAME, C.N_HOST, C.N_MAX_VPS, C.N_MAX_RAM, C.N_MAX_CPU, C.N_MAX_DISK,
                                         C.N_PATH, C.N_BRIDGE, now()))
        log.info("Created default local node '%s'", C.N_NAME)
    if not q("SELECT 1 FROM users WHERE role='admin' LIMIT 1", one=True):
        pw = C.ADMIN_PASS
        if len(pw) < C.MIN_PW or pw == "ChangeMe-Now-123!":
            pw = secrets.token_urlsafe(12)
            log.warning("ADMIN_PASSWORD missing/default. Generated admin password (shown once): %s", pw)
        x("INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
          (C.ADMIN_USER, C.ADMIN_EMAIL, generate_password_hash(pw), "admin", now()))
        log.info("Created admin account '%s'", C.ADMIN_USER)
    apply_session_lifetime()


# ------------------------------------------------------------------ errors & validation
class ApiError(Exception):
    def __init__(s, msg, code=400): s.msg, s.code = msg, code


class LXCError(Exception): pass


UN_RX = r"[A-Za-z0-9_]{3,24}"
EM_RX = r"[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s]{2,}"
HN_RX = r"[a-z]([a-z0-9-]{0,40}[a-z0-9])?"          # VPS / container name
HOSTN_RX = r"[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?"       # hostname inside the OS
LU_RX = r"[a-z_][a-z0-9_-]{1,30}"
NODE_RX = r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}"
HOST_RX = r"[A-Za-z0-9]([A-Za-z0-9.:-]{0,100}[A-Za-z0-9])?"
PATH_RX = r"/[A-Za-z0-9_./-]{0,100}"
BR_RX = r"[A-Za-z0-9_.-]{1,15}"


def body(): return request.get_json(silent=True) or {}


def vstr(d, k, rx=None, mn=1, mx=100, label=None):
    v, label = d.get(k), label or k
    if not isinstance(v, str): raise ApiError(f"{label} is required")
    v = v.strip()
    if not mn <= len(v) <= mx: raise ApiError(f"{label} must be {mn}-{mx} characters")
    if rx and not re.fullmatch(rx, v): raise ApiError(f"{label} has an invalid format")
    return v


def vint(d, k, lo, hi, label=None):
    try: v = int(d.get(k))
    except (TypeError, ValueError): raise ApiError(f"{label or k} must be a number")
    if not lo <= v <= hi: raise ApiError(f"{label or k} must be between {lo} and {hi}")
    return v


def vpass(d, k="password"):
    v = d.get(k)
    if not isinstance(v, str) or not min_pw() <= len(v) <= 128 or re.search(r"[\x00-\x1f]", v):
        raise ApiError(f"Password must be {min_pw()}-128 characters long")
    return v


def chk_confirm(d):
    if "confirm" in d and d.get("confirm") != d.get("password"): raise ApiError("Passwords do not match")


def vimage(d, k="image"):
    v = d.get(k)
    if v not in OS_LIST: raise ApiError("Operating system is not allowed")
    return v.split(":", 1)


ATTEMPTS = {}


def throttle(key, limit=10, window=300):
    t = time.time(); lst = [a for a in ATTEMPTS.get(key, []) if t - a < window]
    if len(lst) >= limit: raise ApiError("Too many attempts. Try again later.", 429)
    lst.append(t); ATTEMPTS[key] = lst


def log_act(action, detail="", vps_id=None, user_id=None, to_id=None):
    try:
        rc = has_request_context()
        uid = user_id if user_id is not None else (g.user["id"] if rc and g.get("user") else None)
        x("INSERT INTO activity(user_id,vps_id,action,detail,ip,created_at) VALUES(?,?,?,?,?,?)",
          (uid, vps_id, action, (detail or "")[:300], request.remote_addr if rc else None, now()))
        actor = None
        if uid:
            ur = q("SELECT username FROM users WHERE id=?", (uid,), one=True); actor = ur["username"] if ur else None
        push_notification(action, detail or "", vps_id, uid, actor, to_id)
    except Exception:
        log.exception("activity log failed")


OPS_ASKED = {"start": "started", "stop": "stopped", "restart": "restarted", "reinstall": "reinstalled", "delete": "deleted",
             "create": "created", "suspend": "suspended", "unsuspend": "unsuspended"}
OPS_DONE = {"create": "is ready", "start": "is running", "stop": "has stopped", "restart": "restarted",
            "reinstall": "was reinstalled", "delete": "was deleted"}
EVENTS = {"auth.login": ("signed in", "auth", "info"), "auth.logout": ("signed out", "auth", "info"),
          "auth.register": ("created an account", "auth", "ok"), "auth.reset": ("reset the password", "auth", "info"),
          "auth.discord.login": ("signed in with Discord", "auth", "info"), "auth.discord.link": ("connected Discord", "auth", "info"),
          "auth.discord.unlink": ("disconnected Discord", "auth", "info"), "auth.sessions.revoke": ("signed out other sessions", "auth", "info"),
          "user.update": ("updated the profile", "auth", "info"), "user.password": ("changed the password", "auth", "info"),
          "admin.settings": ("changed the panel settings", "admin", "info")}


def describe(action, detail, vname):
    """Activity entry -> (text, kind, level), or None when it is not worth a notification."""
    if action in EVENTS: return EVENTS[action]
    m = re.fullmatch(r"vps\.(\w+)\.(done|failed)", action)
    if m:
        op, st = m.groups(); name, _, reason = detail.partition(": "); name = name or vname or "?"
        if st == "done": return (f"VPS {name} {OPS_DONE.get(op, 'finished ' + op)}", "vps", "ok")
        return (f"VPS {name}: {op} failed" + (f" ({reason[:140]})" if reason else ""), "vps", "err")
    m = re.fullmatch(r"vps\.(\w+)", action)
    if m:
        op = m.group(1)
        if op in OPS_ASKED: return (f"{OPS_ASKED[op]} VPS {vname or detail}", "vps", "warn" if op in ("delete", "suspend", "stop") else "info")
        if op == "console": return (f"opened the console of VPS {vname}", "vps", "info") if detail == "terminal opened" else None   # single commands are not announced
        if op == "password": return (f"changed the password of VPS {vname}", "vps", "info")
        if op == "resources": return (f"changed the resources of VPS {vname}", "vps", "info")
    m = re.fullmatch(r"admin\.(user|node)\.(\w+)", action)
    if m:
        what, op = m.groups(); who = detail.split(":")[0]
        words = {"create": "created", "update": "updated", "delete": "deleted", "suspend": "suspended", "unsuspend": "unsuspended"}
        if what == "node": words = {"create": "added", "update": "updated", "delete": "removed"}
        if op == "signout": return (f"signed {who} out everywhere", "admin", "warn")
        if op in words: return (f"{words[op]} {what} {who}", "admin", "warn" if op in ("delete", "suspend") else "info")
        return None
    if action == "admin.vps.stop-all": return (f"stopped all running VPS ({detail})", "admin", "warn")
    return (action.replace(".", " "), "info", "info")


def push_notification(action, detail, vps_id, actor_id, actor, to_id=None):
    """Private by design: a notification goes to the person who did the thing, and to nobody else.
    The result of a background task (ready, running, failed ...) goes to whoever started it."""
    vp = q("SELECT user_id,label,name FROM vps WHERE id=?", (vps_id,), one=True) if vps_id else None
    d = describe(action, detail, (vp["label"] or vp["name"]) if vp else "")
    if not d: return
    text, kind, level = d
    rec = {i for i in (actor_id, to_id) if i}
    if not rec: return
    ref = actor_id or to_id
    if vp and kind == "vps" and vp["user_id"] != ref:                          # acting on somebody else's VPS: say whose
        ow = q("SELECT username FROM users WHERE id=?", (vp["user_id"],), one=True)
        if ow: text += f" · owner {ow['username']}"
    t = now()
    for u in q(f"SELECT id,notif_mute FROM users WHERE id IN ({','.join('?' * len(rec))})", tuple(rec)):
        if level != "err" and kind in (u["notif_mute"] or "").split(","): continue     # muted by that person (failures always get through)
        x("INSERT INTO notifications(user_id,actor_id,actor,text,kind,level,vps_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
          (u["id"], actor_id, actor, text[:300], kind, level, vps_id, t))


def trim_notifications(keep=300):
    x("DELETE FROM notifications WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) rn "
      "FROM notifications) WHERE rn > ?)", (keep,))


# ------------------------------------------------------------------ LXC layer (real commands)
def _cmd(n, args):
    if n["is_local"]: return args
    c = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
         "-p", str(n["ssh_port"])]
    if C.SSH_KEY: c += ["-i", C.SSH_KEY]
    return c + [f'{n["ssh_user"]}@{n["host"]}', " ".join(shlex.quote(a) for a in args)]


def run_on(n, args, input=None, timeout=None, check=True):
    """Run an argv list on a node (locally, or over SSH). Never uses a shell locally."""
    try:
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run(_cmd(n, args), capture_output=True, text=True, errors="replace",
                           timeout=timeout or C.CMD_TIMEOUT, **kw)
    except subprocess.TimeoutExpired:
        t = timeout or C.CMD_TIMEOUT; what = " ".join(args[:5])
        if not check: return 124, f"timed out after {t}s: {what}"
        raise LXCError(f"Timed out after {t}s: {what}")
    except FileNotFoundError as e: raise LXCError(f"Required binary not found: {e.filename} (is LXD installed and on PATH?)")
    if check:
        if p.returncode: raise LXCError(((p.stderr or p.stdout) or "Command failed").strip()[-300:])
        return p.stdout
    return p.returncode, p.stdout + p.stderr


def lxc_exists(n, name): return name in run_on(n, ["lxc", "list", "--format", "csv", "-c", "n"]).split()


def lxc_info(n, name):
    d = json.loads(run_on(n, ["lxc", "query", f"/1.0/instances/{name}/state"]))
    ip = None
    for ifn, nic in (d.get("network") or {}).items():
        if ifn == "lo": continue
        for a in nic.get("addresses", []):
            if a.get("family") == "inet" and a.get("scope") == "global": ip = ip or a.get("address")
    return {"state": str(d.get("status", "")).upper(), "ip": ip,
            "mem_mb": ((d.get("memory") or {}).get("usage") or 0) / 1048576,
            "cpu_s": ((d.get("cpu") or {}).get("usage") or 0) / 1e9}


def attach(n, name, *cmd, input=None, timeout=None, check=True):
    return run_on(n, ["lxc", "exec", name, "--", *cmd], input=input, timeout=timeout, check=check)


def lxc_create(n, v):
    """lxc init images:<os>/<release> <name>  -> the container is named exactly like the VPS."""
    src = f'{C.UBUNTU_REMOTE}:{v["os_version"]}' if v["os"] == "ubuntu" else f'{C.REMOTE}:{v["os"]}/{v["os_version"]}'
    base = ["lxc", "init", src, v["name"],
            "-c", f'limits.memory={v["ram_mb"]}MiB', "-c", f'limits.cpu={v["cpu"]}']
    if n["bridge"]: base += ["-n", n["bridge"]]
    if C.POOL: base += ["-s", C.POOL]
    rc, out = run_on(n, base + ["-d", f'root,size={v["disk_gb"]}GiB'], timeout=C.CREATE_TIMEOUT, check=False)
    if rc and re.search(r"quota|size|disk", out, re.I):  # storage pool without quota support (e.g. dir)
        if lxc_exists(n, v["name"]): run_on(n, ["lxc", "delete", v["name"], "--force"], check=False)
        rc, out = run_on(n, base, timeout=C.CREATE_TIMEOUT, check=False)
    if rc: raise LXCError(out.strip()[-300:])


def lxc_proxy(n, v):
    """Forward <node>:<port> -> container:22 (SSH) using an LXD proxy device."""
    if not v.get("port"): return
    run_on(n, ["lxc", "config", "device", "add", v["name"], "ssh-port", "proxy",
               f'listen=tcp:0.0.0.0:{int(v["port"])}', "connect=tcp:127.0.0.1:22"])


def apply_live(n, v):
    """Apply RAM/CPU/disk limits (works while running). Returns True when the disk limit was applied too."""
    run_on(n, ["lxc", "config", "set", v["name"], f'limits.memory={v["ram_mb"]}MiB', f'limits.cpu={v["cpu"]}'])
    sz = f'size={v["disk_gb"]}GiB'
    rc, _ = run_on(n, ["lxc", "config", "device", "set", v["name"], "root", sz], check=False)
    if rc: rc, _ = run_on(n, ["lxc", "config", "device", "override", v["name"], "root", sz], check=False)
    return rc == 0


def lxc_start(n, name): run_on(n, ["lxc", "start", name], timeout=180)


def lxc_stop(n, name):
    if lxc_info(n, name)["state"] == "STOPPED": return
    rc, _ = run_on(n, ["lxc", "stop", name, "--timeout", "20"], timeout=90, check=False)
    if rc: run_on(n, ["lxc", "stop", name, "--force"], timeout=60)


def destroy_container(n, name):
    if lxc_exists(n, name): run_on(n, ["lxc", "delete", name, "--force"], timeout=120)


def wait_ready(n, name, tries=40):
    for _ in range(tries):
        rc, _ = run_on(n, ["lxc", "exec", name, "--", "true"], check=False, timeout=15)
        if rc == 0: return
        time.sleep(1)
    raise LXCError("Container started but did not become ready in time")


SETUP_DEB = r"""IFS= read -r PW
id "$1" >/dev/null 2>&1 || useradd -m -s /bin/bash "$1"
usermod -aG sudo "$1" 2>/dev/null || usermod -aG wheel "$1" 2>/dev/null || true
printf '%s:%s\n' "$1" "$PW" | chpasswd
printf 'root:%s\n' "$PW" | chpasswd
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication yes\nPermitRootLogin yes\n' > /etc/ssh/sshd_config.d/00-fvm.conf 2>/dev/null || true
echo "$2" > /etc/hostname 2>/dev/null; (hostnamectl set-hostname "$2" || hostname "$2") >/dev/null 2>&1; sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $2/" /etc/hosts 2>/dev/null || true
command -v sshd >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y openssh-server || dnf install -y openssh-server || yum install -y openssh-server || pacman -Sy --noconfirm openssh) >/dev/null 2>&1 || true
(systemctl enable --now ssh || systemctl enable --now sshd || systemctl restart ssh || systemctl restart sshd) >/dev/null 2>&1 || true
exit 0"""
SETUP_ALPINE = r"""IFS= read -r PW
(apk add --no-cache openssh bash sudo >/dev/null 2>&1) || true
id "$1" >/dev/null 2>&1 || adduser -D -s /bin/sh "$1"
printf '%s:%s\n' "$1" "$PW" | chpasswd
printf 'root:%s\n' "$PW" | chpasswd
printf 'PasswordAuthentication yes\nPermitRootLogin yes\n' >> /etc/ssh/sshd_config 2>/dev/null || true
echo "$2" > /etc/hostname 2>/dev/null; hostname "$2" 2>/dev/null || true
(rc-update add sshd default && rc-service sshd restart) >/dev/null 2>&1 || true
exit 0"""
PW_SCRIPT = r"""IFS= read -r PW
printf '%s:%s\n' "$1" "$PW" | chpasswd && printf 'root:%s\n' "$PW" | chpasswd"""


def setup_user(n, v, pw):
    script = SETUP_ALPINE if v["os"] == "alpine" else SETUP_DEB
    try: attach(n, v["name"], "sh", "-c", script, "sh", v["username"], v["hostname"], input=pw + "\n", timeout=300)
    except LXCError as e:
        if str(e).startswith("Timed out"):
            raise LXCError("Setting up the login user inside the VPS timed out. The VPS most likely has no internet access, so the SSH "
                           f"server could not be installed. Check with: lxc exec {v['name']} -- ping -c1 8.8.8.8")
        raise


# ------------------------------------------------------------------ runtime state
POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fvm-task")
METRICS, CPU_PREV, DISK_CACHE = {}, {}, {}
NODE_STATE, HISTORY = {}, {}

VPS_SQL = ("SELECT v.*, u.username AS owner, n.name AS node_name FROM vps v "
           "JOIN users u ON u.id=v.user_id JOIN nodes n ON n.id=v.node_id")


def vps_row(vid): return q(VPS_SQL + " WHERE v.id=?", (vid,), one=True)
def node_of(v): return q("SELECT * FROM nodes WHERE id=?", (v["node_id"],), one=True)


def uptime(started):
    try: return max(0, int((datetime.now(timezone.utc) - datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()))
    except (TypeError, ValueError): return 0


def vps_out(v, adm=False):
    m = METRICS.get(v["id"]) if v["status"] == "running" else None
    d = {k: v[k] for k in ("id", "name", "label", "port", "hostname", "username", "os", "os_version", "ram_mb", "cpu", "disk_gb",
                           "status", "suspended", "ip", "error", "created_at", "user_id", "owner")}
    d["uptime"] = uptime(v["started_at"]) if v["status"] == "running" else 0
    d["mem_mb"] = round(m["mem_mb"]) if m else None
    d["cpu_pct"] = m["cpu_pct"] if m else None
    if adm: d["node"], d["node_id"] = v["node_name"], v["node_id"]
    return d


def sync_state(v, n=None):
    """Read the real state from LXC and persist it."""
    n = n or node_of(v)
    i = lxc_info(n, v["name"])
    st = {"RUNNING": "running", "STOPPED": "stopped"}.get(i["state"])
    if not st: return i
    started = (v.get("started_at") or now()) if st == "running" else None
    x("UPDATE vps SET status=?, ip=?, started_at=? WHERE id=?", (st, i["ip"] if st == "running" else None, started, v["id"]))
    return i


def wait_ip(n, name, tries=25):
    for _ in range(tries):
        i = lxc_info(n, name)
        if i["ip"]: return
        time.sleep(1)


# ------------------------------------------------------------------ background tasks
def spawn(fn, vid, label, *a):
    """Run a VPS task in the background and remember who started it, so the result is reported to them."""
    actor = g.user["id"] if has_request_context() and g.get("user") else None
    POOL.submit(run_task, fn, vid, label, *a, actor_id=actor)


def run_task(fn, vid, label, *a, actor_id=None):
    v0 = vps_row(vid); nm = (v0["label"] or v0["name"]) if v0 else ""; owner = v0["user_id"] if v0 else None
    try:
        res = fn(vid, *a)
        x("UPDATE vps SET error=? WHERE id=?", (res if isinstance(res, str) else None, vid))
        log_act(f"vps.{label}.done", nm, vid, to_id=actor_id or owner)
    except Exception as e:
        log.error("task %s on vps %s failed: %s", label, vid, e)
        msg = str(e)[:300]
        try:
            v = vps_row(vid)
            if v:
                x("UPDATE vps SET error=? WHERE id=?", (msg, vid))
                try: sync_state(v)
                except Exception: pass
                x(f"UPDATE vps SET status='error' WHERE id=? AND status IN ({','.join('?' * len(TRANSIENT))})", (vid, *TRANSIENT))
        except Exception:
            log.exception("could not record task failure")
        log_act(f"vps.{label}.failed", f"{nm}: {msg}", vid, to_id=actor_id or owner)


def w_provision(vid, pw, reinstall=False):
    v = vps_row(vid); n = node_of(v)
    if reinstall: destroy_container(n, v["name"])
    lxc_create(n, v); lxc_proxy(n, v)
    lxc_start(n, v["name"]); wait_ready(n, v["name"]); setup_user(n, v, pw)
    wait_ip(n, v["name"])
    x("UPDATE vps SET status='running', started_at=? WHERE id=?", (now(), vid))
    sync_state(vps_row(vid), n)
    return check_ssh(n, v)


SSH_UP = r"grep -qiE ':0016 [0-9A-F:]+ 0A' /proc/net/tcp /proc/net/tcp6 2>/dev/null"


def check_ssh(n, v):
    """Verify SSH really answers. Returns None when fine, else a short explanation shown on the VPS page."""
    import socket
    name, port = v["name"], v.get("port")
    probe = f"for i in 1 2 3 4 5 6; do {SSH_UP} && exit 0; sleep 2; done; exit 1"
    try:
        if attach(n, name, "sh", "-c", probe, timeout=45, check=False)[0]:
            attach(n, name, "sh", "-c", "(systemctl enable --now ssh || systemctl enable --now sshd || rc-service sshd start) >/dev/null 2>&1; true", timeout=45, check=False)
            if attach(n, name, "sh", "-c", probe, timeout=45, check=False)[0]:
                return ("SSH is not running inside this VPS. Usually the SSH server is not installed and the VPS has no internet "
                        "access to install it. Test with: lxc exec " + name + " -- ping -c1 8.8.8.8")
        if port and n["is_local"]:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as c:
                    c.settimeout(5)
                    if not c.recv(32).startswith(b"SSH-"): return "Port forwarding is up but the VPS did not answer like an SSH server."
            except OSError:
                return f"SSH runs inside the VPS but forwarding on port {port} is not working on the server."
    except Exception as e:
        log.debug("ssh check failed: %s", e)
    return None


def w_start(vid):
    v = vps_row(vid); n = node_of(v)
    if v["suspended"]: raise LXCError("VPS is suspended")
    lxc_start(n, v["name"]); wait_ready(n, v["name"]); wait_ip(n, v["name"])
    x("UPDATE vps SET started_at=? WHERE id=?", (now(), vid)); sync_state(vps_row(vid), n)


def w_stop(vid):
    v = vps_row(vid); n = node_of(v); lxc_stop(n, v["name"]); sync_state(v, n)


def w_restart(vid):
    v = vps_row(vid); n = node_of(v)
    if v["suspended"]: raise LXCError("VPS is suspended")
    lxc_stop(n, v["name"]); lxc_start(n, v["name"]); wait_ready(n, v["name"]); wait_ip(n, v["name"])
    x("UPDATE vps SET started_at=? WHERE id=?", (now(), vid)); sync_state(vps_row(vid), n)


def w_delete(vid):
    v = vps_row(vid)
    if not v: return
    destroy_container(node_of(v), v["name"])
    x("DELETE FROM vps WHERE id=?", (vid,)); METRICS.pop(vid, None); CPU_PREV.pop(vid, None)


def claim(vid, st):
    ph = ",".join("?" * len(TRANSIENT))
    if x(f"UPDATE vps SET status=?, error=NULL WHERE id=? AND status NOT IN ({ph})", (st, vid, *TRANSIENT)).rowcount == 0:
        raise ApiError("Another operation is already running on this VPS", 409)


def bg_stop(vid):
    try:
        claim(vid, "stopping"); spawn(w_stop, vid, "stop")
    except ApiError: pass


# ------------------------------------------------------------------ node stats + monitor
def node_stats(n):
    if n["is_local"]:
        vm = psutil.virtual_memory(); path = n["lxc_path"] if os.path.isdir(n["lxc_path"]) else "/"
        du = psutil.disk_usage(path)
        return {"cpu": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count(), "ram_total": vm.total,
                "ram_used": vm.total - vm.available, "disk_total": du.total, "disk_used": du.used,
                "load": os.getloadavg()[0], "uptime": int(time.time() - psutil.boot_time())}
    script = ('cat /proc/loadavg /proc/uptime; nproc; grep -E "^(MemTotal|MemAvailable):" /proc/meminfo; '
              'df -Pk "$0" | tail -1')
    out = run_on(n, ["sh", "-c", script, n["lxc_path"]], timeout=20).splitlines()
    load = float(out[0].split()[0]); up = int(float(out[1].split()[0])); cores = int(out[2])
    mt = int(out[3].split()[1]) * 1024; ma = int(out[4].split()[1]) * 1024; df = out[5].split()
    return {"cpu": min(100.0, load / max(cores, 1) * 100), "cores": cores, "ram_total": mt, "ram_used": mt - ma,
            "disk_total": int(df[1]) * 1024, "disk_used": int(df[2]) * 1024, "load": load, "uptime": up}


def refresh_node(n):
    try:
        s = node_stats(n)
        NODE_STATE[n["id"]] = {"online": True, "error": None, "ts": now(), **s}
        HISTORY.setdefault(n["id"], deque(maxlen=C.HIST)).append(
            {"t": now(), "cpu": round(s["cpu"], 1), "ram": round(s["ram_used"] / max(s["ram_total"], 1) * 100, 1),
             "disk": round(s["disk_used"] / max(s["disk_total"], 1) * 100, 1)})
        return True
    except Exception as e:
        NODE_STATE[n["id"]] = {"online": False, "error": str(e)[:200], "ts": now()}
        return False


def monitor_vps(n, v):
    i = lxc_info(n, v["name"]); t = time.time()
    st = {"RUNNING": "running", "STOPPED": "stopped"}.get(i["state"])
    if not st: return
    if st == "running":
        prev = CPU_PREV.get(v["id"]); pct = 0.0
        if prev and t > prev[1] and i["cpu_s"] >= prev[0]:
            pct = min(100.0, (i["cpu_s"] - prev[0]) / (t - prev[1]) / max(v["cpu"], 1) * 100)
        CPU_PREV[v["id"]] = (i["cpu_s"], t)
        METRICS[v["id"]] = {"mem_mb": i["mem_mb"], "cpu_pct": round(pct, 1)}
    else:
        METRICS.pop(v["id"], None); CPU_PREV.pop(v["id"], None)
    started = (v["started_at"] or now()) if st == "running" else None
    ip = i["ip"] if st == "running" else None
    if (st, ip, started) != (v["status"], v["ip"], v["started_at"]):
        x("UPDATE vps SET status=?, ip=?, started_at=? WHERE id=? AND status=?", (st, ip, started, v["id"], v["status"]))
        if st == "running" and v["status"] in ("stopped", "error"):      # it is up after all: an old failure message no longer applies
            x("UPDATE vps SET error=NULL WHERE id=?", (v["id"],))


def monitor_once():
    for n in q("SELECT * FROM nodes"):
        if not refresh_node(n): continue
        for v in q(f"SELECT * FROM vps WHERE node_id=? AND status NOT IN ({','.join('?' * len(TRANSIENT))})", (n["id"], *TRANSIENT)):
            try: monitor_vps(n, v)
            except Exception as e: log.debug("monitor %s: %s", v["name"], e)


def monitor_loop():
    psutil.cpu_percent(interval=None); last_trim = 0.0
    while True:
        try: monitor_once()
        except Exception: log.exception("monitor error")
        if time.time() - last_trim > 600:
            last_trim = time.time()
            try: trim_notifications()
            except Exception: log.exception("notification cleanup failed")
        time.sleep(C.MON_INT)


def node_usage(nid):
    return q("SELECT COUNT(*) n, COALESCE(SUM(ram_mb),0) ram, COALESCE(SUM(cpu),0) cpu, COALESCE(SUM(disk_gb),0) disk "
             "FROM vps WHERE node_id=?", (nid,), one=True)


def node_fits(n, ram, cpu, disk, skip=None):
    u = node_usage(n["id"])
    if skip: u = {"n": u["n"] - 1, "ram": u["ram"] - skip["ram_mb"], "cpu": u["cpu"] - skip["cpu"], "disk": u["disk"] - skip["disk_gb"]}
    return ((not n["max_vps"] or u["n"] + 1 <= n["max_vps"]) and (not n["max_ram_mb"] or u["ram"] + ram <= n["max_ram_mb"])
            and (not n["max_cpu"] or u["cpu"] + cpu <= n["max_cpu"]) and (not n["max_disk_gb"] or u["disk"] + disk <= n["max_disk_gb"]))


def pick_node(ram, cpu, disk, node_id=None):
    if node_id:
        n = q("SELECT * FROM nodes WHERE id=?", (node_id,), one=True)
        if not n or not n["enabled"]: raise ApiError("Selected node is unavailable")
        if not node_fits(n, ram, cpu, disk): raise ApiError("Selected node does not have enough capacity")
        return n
    best = None
    for n in q("SELECT * FROM nodes WHERE enabled=1"):
        if NODE_STATE.get(n["id"], {}).get("online") is False: continue
        if node_fits(n, ram, cpu, disk):
            c = node_usage(n["id"])["n"]
            if best is None or c < best[0]: best = (c, n)
    if not best: raise ApiError("No node has capacity for this VPS", 503)
    return best[1]


# ------------------------------------------------------------------ Flask app
app = Flask(__name__)
app.secret_key = C.SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=C.COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=C.SESSION_HOURS), MAX_CONTENT_LENGTH=64 * 1024)
if C.PROXY: app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.errorhandler(ApiError)
def _ae(e): return jsonify({"error": e.msg}), e.code


@app.errorhandler(LXCError)
def _le(e): return jsonify({"error": f"LXC error: {e}"}), 502


@app.errorhandler(HTTPException)
def _he(e):
    if request.path.startswith("/api"): return jsonify({"error": e.description}), e.code
    return e


@app.errorhandler(Exception)
def _ex(e):
    log.exception("Unhandled error"); return jsonify({"error": "Internal server error"}), 500


@app.before_request
def _pre():
    g.user = None
    uid = session.get("uid")
    if uid:
        u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
        sid = session.get("sid")
        row = q("SELECT last_seen FROM sessions WHERE sid=? AND user_id=?", (sid, uid), one=True) if sid else None
        if u and not u["suspended"] and row and row["last_seen"] >= later(-session_hours() * 60):
            g.user = u
            if row["last_seen"] < later(-1): x("UPDATE sessions SET last_seen=? WHERE sid=?", (now(), sid))
        else:
            if sid: x("DELETE FROM sessions WHERE sid=?", (sid,))
            session.clear()
    if request.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE", "PATCH"):
        t, s = request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")
        if not s or not secrets.compare_digest(t.encode(), s.encode()):
            raise ApiError("Security token expired. Reload the page and try again.", 403)


@app.after_request
def _post(r):
    r.headers["X-Powered-By"] = "FVM Panel"
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                      "Cache-Control": "no-store"})
    return r


def api(path, methods=("GET",), role="user"):
    def deco(fn):
        def w(*a, **k):
            if role != "public":
                if not g.user: raise ApiError("Authentication required", 401)
                if role == "admin" and g.user["role"] != "admin": raise ApiError("Administrator access required", 403)
            return jsonify(_call(fn, a, k))
        w.__name__ = fn.__name__
        app.add_url_rule("/api" + path, fn.__name__, w, methods=list(methods))
        return fn
    return deco


def _call(fn, a, k):
    r = fn(*a, **k)
    return {"ok": True} if r is None else r


def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return session["csrf"]


def login_user(uid):
    session.clear(); session["uid"] = uid; session["csrf"] = secrets.token_urlsafe(32); session.permanent = True
    sid = session["sid"] = secrets.token_urlsafe(24); t = now()
    x("INSERT INTO sessions(sid,user_id,created_at,last_seen,ip) VALUES(?,?,?,?,?)", (sid, uid, t, t, request.remote_addr))
    x("DELETE FROM sessions WHERE last_seen < ?", (later(-session_hours() * 60),))          # expired
    others = [r["sid"] for r in q("SELECT sid FROM sessions WHERE user_id=? AND sid<>? ORDER BY last_seen DESC, rowid DESC", (uid, sid))]
    keep = [sid] + others[:max_sessions() - 1]          # the session being created is always kept
    x(f"DELETE FROM sessions WHERE user_id=? AND sid NOT IN ({','.join('?' * len(keep))})", (uid, *keep))   # enforce the per-user cap
    x("UPDATE users SET last_login=? WHERE id=?", (t, uid))
    return session["csrf"]


def user_out(u):
    d = {k: u[k] for k in ("id", "username", "email", "role", "suspended", "created_at", "last_login", "discord_name")}
    d["has_password"] = u["password_hash"] != "!"          # accounts created through Discord start without a password
    return d


# ---- auth
@api("/config", role="public")
def config():
    return {"name": setting("panel_name"), "version": C.VERSION, "registration": setting("registration_enabled") == "1",
            "csrf": csrf_token(), "cdn": C.CDN, "console": console_on(), "logo": setting("logo_text"), "accent": setting("accent"),
            "title": setting("panel_title") or setting("panel_name"), "description": setting("site_description"),
            "tagline": setting("panel_description"), "icon": setting("site_icon"), "discord_login": discord_ready(),
            "user": user_out(g.user) if g.user else None,
            "os": [{"id": o, "name": OS_NAMES.get(o.split(":")[0], o.split(":")[0].title()), "version": o.split(":")[1],
                    "label": OS_NAMES.get(o.split(":")[0], o.split(":")[0].title()) + " " + o.split(":")[1]} for o in OS_LIST],
            "os_names": OS_NAMES,
            "defaults": {"ram": C.RAM, "cpu": C.CPU, "disk": C.DISK, "max_ram": C.MAX_RAM, "max_cpu": C.MAX_CPU,
                         "max_disk": C.MAX_DISK, "min_pw": min_pw(), "port_min": C.PORT_MIN, "port_max": C.PORT_MAX}}


DUMMY = generate_password_hash("fvm-dummy-password")


@api("/login", ["POST"], "public")
def login():
    d = body(); ip = request.remote_addr; throttle("login:" + str(ip), login_limit(), 300)
    ident, pw = str(d.get("identity", "")).strip(), str(d.get("password", ""))
    u = q("SELECT * FROM users WHERE username=? OR email=?", (ident, ident), one=True)
    ok = check_password_hash(u["password_hash"] if u else DUMMY, pw)
    if not (u and ok): raise ApiError("Invalid username or password", 401)
    if u["suspended"]: raise ApiError("This account is suspended. Contact an administrator.", 403)
    ATTEMPTS.pop("login:" + str(ip), None)
    csrf = login_user(u["id"]); log_act("auth.login", "", user_id=u["id"])
    return {"csrf": csrf, "user": user_out(u)}


@api("/register", ["POST"], "public")
def register():
    if setting("registration_enabled") != "1": raise ApiError("Registration is disabled", 403)
    throttle("reg:" + str(request.remote_addr), 5, 3600)
    d = body(); chk_confirm(d); un = vstr(d, "username", UN_RX, 3, 24, "Username"); em = vstr(d, "email", EM_RX, 5, 254, "Email").lower()
    p = vpass(d)
    try:
        uid = x("INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)",
                (un, em, generate_password_hash(p), now())).lastrowid
    except sqlite3.IntegrityError: raise ApiError("Username or email is already in use", 409)
    csrf = login_user(uid); log_act("auth.register", un, user_id=uid)
    return {"csrf": csrf, "user": user_out(q("SELECT * FROM users WHERE id=?", (uid,), one=True))}


@api("/logout", ["POST"])
def logout():
    log_act("auth.logout"); x("DELETE FROM sessions WHERE sid=?", (session.get("sid"),)); session.clear(); return {"csrf": csrf_token()}


@api("/reset", ["POST"], "public")
def reset_password():
    throttle("reset:" + str(request.remote_addr), 10, 600)
    d = body(); chk_confirm(d); th = hashlib.sha256(str(d.get("token", "")).encode()).hexdigest(); p = vpass(d)
    t = q("SELECT * FROM reset_tokens WHERE token_hash=? AND used=0 AND expires_at>?", (th, now()), one=True)
    if not t: raise ApiError("This reset link is invalid or has expired", 400)
    x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(p), t["user_id"]))
    x("UPDATE reset_tokens SET used=1 WHERE user_id=?", (t["user_id"],)); log_act("auth.reset", "", user_id=t["user_id"])


@api("/me", ["PUT"])
def me_update():
    em = vstr(body(), "email", EM_RX, 5, 254, "Email").lower()
    try: x("UPDATE users SET email=? WHERE id=?", (em, g.user["id"]))
    except sqlite3.IntegrityError: raise ApiError("Email is already in use", 409)
    log_act("user.update", "email"); return {"user": user_out(q("SELECT * FROM users WHERE id=?", (g.user["id"],), one=True))}


@api("/me/password", ["POST"])
def me_password():
    d = body(); chk_confirm(d)
    if g.user["password_hash"] != "!" and not check_password_hash(g.user["password_hash"], str(d.get("current", ""))): raise ApiError("Current password is incorrect", 400)
    x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(vpass(d, "password")), g.user["id"]))
    log_act("user.password")


@api("/overview")
def overview():
    uid = g.user["id"]; vs = q(VPS_SQL + " WHERE v.user_id=? ORDER BY v.id DESC", (uid,))
    run = [v for v in vs if v["status"] == "running"]; ms = [METRICS.get(v["id"]) for v in run]; ms = [m for m in ms if m]
    return {"user": user_out(g.user),
            "stats": {"total": len(vs), "running": len(run), "stopped": sum(v["status"] == "stopped" for v in vs)},
            "usage": {"ram": sum(v["ram_mb"] for v in vs), "cpu": sum(v["cpu"] for v in vs), "disk": sum(v["disk_gb"] for v in vs),
                      "mem_used": round(sum(m["mem_mb"] for m in ms)),
                      "cpu_avg": round(sum(m["cpu_pct"] for m in ms) / len(ms), 1) if ms else 0},
            "vps": [vps_out(v) for v in vs[:4]],
            "activity": q("SELECT action,detail,created_at FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,))}


# ---- VPS
def owned(vid):
    v = vps_row(vid)
    if not v or (g.user["role"] != "admin" and v["user_id"] != g.user["id"]): raise ApiError("VPS not found", 404)
    return v


@api("/vps")
def vps_list():
    return [vps_out(v, g.user["role"] == "admin") for v in q(VPS_SQL + " WHERE v.user_id=? ORDER BY v.id DESC", (g.user["id"],))]


def port_free_local(port):
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try: sk.bind(("0.0.0.0", port)); return True
    except PermissionError: return True      # not root: cannot test, let LXD decide
    except OSError: return False
    finally: sk.close()


def create_vps(owner, d):
    img = vimage(d); label = vstr(d, "name", None, 1, 60, "VPS name")
    if re.search(r"[\x00-\x1f<>]", label): raise ApiError("VPS name contains invalid characters")
    hn = name = vstr(d, "hostname", HN_RX, 1, 42, "Hostname").lower()   # hostname IS the LXD container name
    un = vstr(d, "username", LU_RX, 2, 31, "Username")
    if un in ("root", "admin", "daemon", "bin", "sys"): raise ApiError("That username is reserved")
    chk_confirm(d); pw = vpass(d)
    port = vint(d, "port", C.PORT_MIN, C.PORT_MAX, "Port")
    if port == C.PORT: raise ApiError("That port is reserved by the panel")
    ram, cpu = vint(d, "ram_mb", 128, C.MAX_RAM, "RAM"), vint(d, "cpu", 1, C.MAX_CPU, "CPU cores")
    disk = vint(d, "disk_gb", 1, C.MAX_DISK, "Disk")
    nid = int(d["node_id"]) if str(d.get("node_id") or "").isdigit() else None
    n = pick_node(ram, cpu, disk, nid)
    if q("SELECT 1 FROM vps WHERE node_id=? AND port=?", (n["id"], port), one=True): raise ApiError("That port is already used by another VPS on this node", 409)
    if n["is_local"] and not port_free_local(port): raise ApiError("That port is already in use on the server", 409)
    if q("SELECT 1 FROM vps WHERE name=?", (name,), one=True): raise ApiError("That hostname is already in use. Pick a different one.", 409)
    vid = x("INSERT INTO vps(name,label,port,user_id,node_id,hostname,username,os,os_version,ram_mb,cpu,disk_gb,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'creating',?)", (name, label, port, owner["id"], n["id"], hn, un, img[0], img[1], ram, cpu, disk, now())).lastrowid
    log_act("vps.create", f"{label} [{hn}] ({img[0]}:{img[1]}) for {owner['username']}", vid)
    spawn(w_provision, vid, "create", pw)
    return {"id": vid, "message": "VPS is being created. This can take a few minutes."}


@api("/vps/<int:vid>")
def vps_get(vid):
    v = owned(vid); d = vps_out(v, g.user["role"] == "admin"); d["disk_used_mb"] = None
    if v["status"] == "running":
        c = DISK_CACHE.get(vid)
        if c and time.time() - c[1] < 30: d["disk_used_mb"] = c[0]
        else:
            try:
                row = attach(node_of(v), v["name"], "df", "-Pk", "/", timeout=10).strip().splitlines()[-1].split()
                d["disk_used_mb"] = round(int(row[2]) / 1024); DISK_CACHE[vid] = (d["disk_used_mb"], time.time())
            except Exception: pass
    return d


@api("/vps/<int:vid>/<any(start,stop,restart):op>", ["POST"])
def vps_op(vid, op):
    v = owned(vid)
    if v["suspended"] and op != "stop": raise ApiError("This VPS is suspended. Contact an administrator.", 403)
    if op == "start" and v["status"] == "running": raise ApiError("VPS is already running")
    if op == "stop" and v["status"] == "stopped": raise ApiError("VPS is already stopped")
    claim(vid, {"start": "starting", "stop": "stopping", "restart": "restarting"}[op])
    log_act("vps." + op, v["hostname"], vid)
    spawn({"start": w_start, "stop": w_stop, "restart": w_restart}[op], vid, op)
    return {"message": f"{op.title()} requested"}


@api("/vps/<int:vid>/reinstall", ["POST"])
def vps_reinstall(vid):
    v = owned(vid); d = body()
    if v["suspended"]: raise ApiError("This VPS is suspended", 403)
    img = vimage(d); pw = vpass(d)
    claim(vid, "reinstalling")
    x("UPDATE vps SET os=?, os_version=?, ip=NULL, started_at=NULL WHERE id=?", (img[0], img[1], vid))
    log_act("vps.reinstall", f"{v['hostname']} -> {d['image']}", vid)
    spawn(w_provision, vid, "reinstall", pw, True)
    return {"message": "Reinstall started. All data on this VPS will be erased."}


@api("/vps/<int:vid>", ["DELETE"])
def vps_delete(vid):
    v = owned(vid); claim(vid, "deleting"); log_act("vps.delete", v["hostname"], vid)
    spawn(w_delete, vid, "delete"); return {"message": "Deleting VPS"}


@api("/vps/<int:vid>/password", ["POST"])
def vps_password(vid):
    v = owned(vid); pw = vpass(body())
    if v["status"] != "running": raise ApiError("Start the VPS before changing its password")
    attach(node_of(v), v["name"], "sh", "-c", PW_SCRIPT, "sh", v["username"], input=pw + "\n", timeout=30)
    log_act("vps.password", v["hostname"], vid); return {"message": "Password updated"}


@api("/vps/<int:vid>/exec", ["POST"])
def vps_exec(vid):
    v = owned(vid)
    if not console_on(): raise ApiError("Console is disabled", 403)
    if v["suspended"]: raise ApiError("This VPS is suspended", 403)
    if v["status"] != "running": raise ApiError("VPS is not running")
    cmd = vstr(body(), "command", None, 1, 2000, "Command")
    if "\x00" in cmd: raise ApiError("Invalid command")
    log_act("vps.console", cmd[:120], vid)
    rc, out = attach(node_of(v), v["name"], "sh", "-lc", cmd, timeout=25, check=False)
    return {"output": out[-20000:], "code": rc}


@api("/vps/<int:vid>/<any(suspend,unsuspend):op>", ["POST"], "admin")
def vps_suspend(vid, op):
    v = owned(vid); flag = 1 if op == "suspend" else 0
    x("UPDATE vps SET suspended=? WHERE id=?", (flag, vid)); log_act("vps." + op, v["hostname"], vid)
    if flag and v["status"] == "running": bg_stop(vid)
    return {"message": f"VPS {op}ed"}


@api("/vps/<int:vid>/resources", ["PUT"], "admin")
def vps_resources(vid):
    v = owned(vid); d = body()
    ram, cpu, disk = vint(d, "ram_mb", 128, C.MAX_RAM, "RAM"), vint(d, "cpu", 1, C.MAX_CPU, "CPU cores"), vint(d, "disk_gb", 1, C.MAX_DISK, "Disk")
    n = node_of(v)
    if not node_fits(n, ram, cpu, disk, skip=v): raise ApiError("Node does not have enough capacity for these resources")
    nv = {**v, "ram_mb": ram, "cpu": cpu, "disk_gb": disk}
    disk_ok = apply_live(n, nv)
    x("UPDATE vps SET ram_mb=?, cpu=?, disk_gb=? WHERE id=?", (ram, cpu, disk, vid))
    log_act("vps.resources", f"{ram}MB/{cpu}cpu/{disk}GB", vid)
    note = "" if disk_ok else " RAM and CPU applied. The storage pool does not support disk quotas, so disk size is only recorded."
    return {"message": "Resources updated." + note}



# ---- interactive web terminal (real PTY -> `lxc exec`, streamed over HTTP long-polling)
TERMS = {}
SHELL_CMD = "command -v bash >/dev/null 2>&1 && exec bash -l || exec sh -l"


class Term:
    def __init__(s, uid, vid, cmd, cols, rows, password=None):
        s.pw = (password + "\n").encode() if password else None
        s.uid, s.vid, s.cond, s.buf, s.start, s.alive = uid, vid, threading.Condition(), bytearray(), 0, True
        s.last_in = s.last_poll = time.time()
        m, sl = pty.openpty(); s.fd = m
        fcntl.ioctl(m, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            s.proc = subprocess.Popen(cmd, stdin=sl, stdout=sl, stderr=sl, close_fds=True, start_new_session=True,
                                      env={**os.environ, "TERM": "xterm-256color"})
        except Exception:
            os.close(m); raise
        finally: os.close(sl)
        threading.Thread(target=s._pump, daemon=True, name="fvm-term").start()

    def _pump(s):
        tail = b""
        while True:
            try:
                r, _, _ = select.select([s.fd], [], [], 1.0)
                if not r:
                    if s.proc.poll() is not None: break
                    continue
                d = os.read(s.fd, 65536)
                if not d: break
            except OSError: break
            if s.pw:  # answer ssh's single password prompt, then forget the password
                tail = (tail + d)[-160:]
                if re.search(rb"[Pp]assword[^:\n]*:\s*$", tail):
                    try: os.write(s.fd, s.pw)
                    except OSError: pass
                    s.pw = None
            with s.cond:
                s.buf += d
                if len(s.buf) > 1048576:
                    cut = len(s.buf) // 2; del s.buf[:cut]; s.start += cut
                s.cond.notify_all()
        with s.cond: s.alive = False; s.cond.notify_all()
        try: s.proc.wait(timeout=5)
        except Exception: s.proc.kill()
        try: os.close(s.fd)
        except OSError: pass

    def read(s, off, wait=20.0):
        s.last_poll = time.time(); end = time.time() + wait
        with s.cond:
            while s.alive and s.start + len(s.buf) <= off:
                left = end - time.time()
                if left <= 0: break
                s.cond.wait(left)
            off = max(off, s.start)
            return bytes(s.buf[off - s.start:]), s.start + len(s.buf), s.alive

    def write(s, data):
        if not s.alive: raise ApiError("Terminal session has ended", 410)
        s.last_in = time.time()
        try: os.write(s.fd, data)
        except OSError: raise ApiError("Terminal session has ended", 410)

    def resize(s, cols, rows):
        if not s.alive: return
        try:
            fcntl.ioctl(s.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.killpg(s.proc.pid, signal.SIGWINCH)
        except OSError: pass

    def close(s):
        try: os.killpg(s.proc.pid, signal.SIGTERM)
        except OSError: pass


def term_reaper():
    while True:
        time.sleep(15); t = time.time()
        for sid, tm in list(TERMS.items()):
            if (t - tm.last_poll > 45) or (t - tm.last_in > C.TERM_IDLE) or (not tm.alive and t - tm.last_poll > 10):
                tm.close(); TERMS.pop(sid, None)


def get_term(sid):
    tm = TERMS.get(sid)
    if not tm or tm.uid != g.user["id"]: raise ApiError("Terminal session not found", 404)
    return tm


def _dim(d, k, dflt):
    try: return min(max(int(d.get(k, dflt)), 5), 500)
    except (TypeError, ValueError): return dflt


@api("/vps/<int:vid>/term", ["POST"])
def term_open(vid):
    v = owned(vid)
    if not console_on(): raise ApiError("Console is disabled", 403)
    if v["suspended"]: raise ApiError("This VPS is suspended", 403)
    if v["status"] != "running": raise ApiError("VPS is not running")
    if sum(1 for t in TERMS.values() if t.uid == g.user["id"] and t.alive) >= C.TERM_MAX:
        raise ApiError(f"Too many open consoles (limit {C.TERM_MAX}). Close one and try again.", 429)
    d = body(); n = node_of(v); pwd = None; mode = d.get("mode", "shell")
    if mode == "ssh":
        if not v["ip"]: raise ApiError("This VPS has no IP address yet")
        if d.get("host") and str(d["host"]).strip() != v["ip"]: raise ApiError("Host must be this VPS's IP address")
        user = vstr(d, "username", LU_RX, 2, 31, "Username"); port = vint(d, "port", 1, 65535, "Port")
        pwd = d.get("password")
        if not isinstance(pwd, str) or not pwd or len(pwd) > 128 or re.search(r"[\x00-\x1f]", pwd): raise ApiError("Enter the VPS password")
        cmd = _cmd(n, ["ssh", "-tt", "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                       "-o", "LogLevel=ERROR", "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                       "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "ConnectTimeout=8", f"{user}@{v['ip']}"])
    elif mode == "shell":
        cmd = _cmd(n, ["lxc", "exec", v["name"], "--env", "TERM=xterm-256color", "--env", "LANG=C.UTF-8", "--",
                       "sh", "-c", SHELL_CMD])
    else: raise ApiError("Unknown connection mode")
    if not n["is_local"]: cmd.insert(1, "-tt")
    sid = secrets.token_urlsafe(24)
    try: TERMS[sid] = Term(g.user["id"], vid, cmd, _dim(d, "cols", 80), _dim(d, "rows", 24), pwd)
    except FileNotFoundError as e: raise ApiError(f"Required binary not found: {e.filename}", 502)
    log_act("vps.console", "ssh session opened" if mode == "ssh" else "quick shell opened", vid)
    return {"sid": sid}


@api("/term/<sid>/read")
def term_read(sid):
    tm = get_term(sid)
    try: off = max(0, int(request.args.get("o", "0")))
    except ValueError: off = 0
    data, new, alive = tm.read(off)
    return {"data": base64.b64encode(data).decode(), "o": new, "alive": alive}


@api("/term/<sid>/write", ["POST"])
def term_write(sid):
    d = body().get("d")
    if not isinstance(d, str) or not d: raise ApiError("No input")
    get_term(sid).write(d.encode("utf-8", "ignore"))
    return {"ok": True}


@api("/term/<sid>/resize", ["POST"])
def term_resize(sid):
    d = body(); get_term(sid).resize(_dim(d, "cols", 80), _dim(d, "rows", 24)); return {"ok": True}


@api("/term/<sid>", ["DELETE"])
def term_close(sid):
    tm = get_term(sid); tm.close(); TERMS.pop(sid, None); return {"ok": True}


def sess_id(sid): return hashlib.sha256(sid.encode()).hexdigest()[:12]      # short public id; the real session id never leaves the server


@api("/me/sessions")
def me_sessions():
    cur = session.get("sid")
    return [{"id": sess_id(r["sid"]), "created_at": r["created_at"], "last_seen": r["last_seen"], "ip": r["ip"], "current": r["sid"] == cur}
            for r in q("SELECT sid,created_at,last_seen,ip FROM sessions WHERE user_id=? ORDER BY last_seen DESC", (g.user["id"],))]


@api("/me/sessions/revoke", ["POST"])
def me_sessions_revoke():
    d = body(); cur = session.get("sid"); n = 0
    for r in q("SELECT sid FROM sessions WHERE user_id=?", (g.user["id"],)):
        if r["sid"] != cur and (d.get("others") or d.get("id") == sess_id(r["sid"])):
            x("DELETE FROM sessions WHERE sid=?", (r["sid"],)); n += 1
    log_act("auth.sessions.revoke", f"{n} session(s)")
    return {"message": f"Signed out {n} session(s)" if n else "Nothing to sign out", "count": n}


def _unread(uid): return q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read=0", (uid,), one=True)["c"]
def _latest(uid): return q("SELECT MAX(id) m FROM notifications WHERE user_id=?", (uid,), one=True)["m"] or 0


@api("/notifications")
def notif_list():
    uid = g.user["id"]
    try: lim = min(max(int(request.args.get("limit", 50)), 1), 300)
    except ValueError: lim = 50
    sql, args = "SELECT id,actor_id,actor,text,kind,level,vps_id,created_at,read FROM notifications WHERE user_id=?", [uid]
    if request.args.get("unread") == "1": sql += " AND read=0"
    if request.args.get("kind") in ("vps", "auth", "admin"): sql += " AND kind=?"; args.append(request.args["kind"])
    if (request.args.get("since") or "").isdigit(): sql += " AND id>?"; args.append(int(request.args["since"]))
    return {"items": q(sql + " ORDER BY id DESC LIMIT ?", (*args, lim)), "unread": _unread(uid), "latest": _latest(uid)}


@api("/notifications/count")
def notif_count(): return {"unread": _unread(g.user["id"]), "latest": _latest(g.user["id"])}


@api("/notifications/read", ["POST"])
def notif_read():
    d = body(); uid = g.user["id"]
    if d.get("all"): x("UPDATE notifications SET read=1 WHERE user_id=?", (uid,))
    else:
        ids = [int(i) for i in (d.get("ids") or []) if str(i).isdigit()][:200]
        if ids: x(f"UPDATE notifications SET read=1 WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", (uid, *ids))
    return {"unread": _unread(uid)}


@api("/notifications/clear", ["POST"])
def notif_clear():
    x("DELETE FROM notifications WHERE user_id=?", (g.user["id"],)); return {"unread": 0}


@api("/notifications/<int:nid>", ["DELETE"])
def notif_delete(nid):
    x("DELETE FROM notifications WHERE id=? AND user_id=?", (nid, g.user["id"])); return {"unread": _unread(g.user["id"])}


@api("/notifications/prefs")
def notif_prefs_get(): return {"muted": [k for k in (g.user["notif_mute"] or "").split(",") if k]}


@api("/notifications/prefs", ["PUT"])
def notif_prefs_put():
    muted = sorted({k for k in (body().get("muted") or []) if k in ("vps", "auth", "admin")})
    x("UPDATE users SET notif_mute=? WHERE id=?", (",".join(muted), g.user["id"])); return {"muted": muted}


# ---- admin: overview, users
@api("/admin/overview", role="admin")
def a_overview():
    vs = q("SELECT status FROM vps"); nodes = q("SELECT id FROM nodes")
    on = [NODE_STATE[n["id"]] for n in nodes if NODE_STATE.get(n["id"], {}).get("online")]
    tot = lambda k: sum(s[k] for s in on)
    return {"users": q("SELECT COUNT(*) c FROM users", one=True)["c"], "vps": len(vs),
            "running": sum(v["status"] == "running" for v in vs), "stopped": sum(v["status"] == "stopped" for v in vs),
            "nodes": len(nodes), "nodes_online": len(on),
            "system": {"cpu": round(sum(s["cpu"] for s in on) / len(on), 1) if on else 0, "ram_used": tot("ram_used"),
                       "ram_total": tot("ram_total"), "disk_used": tot("disk_used"), "disk_total": tot("disk_total")},
            "activity": q("SELECT a.action,a.detail,a.created_at,u.username FROM activity a LEFT JOIN users u ON u.id=a.user_id "
                          "ORDER BY a.id DESC LIMIT 10")}


@api("/admin/users", role="admin")
def a_users():
    return [{**user_out(u), "vps_count": u["vps_count"]} for u in
            q("SELECT u.*, (SELECT COUNT(*) FROM vps WHERE user_id=u.id) AS vps_count FROM users u ORDER BY u.id")]


@api("/admin/users", ["POST"], "admin")
def a_user_create():
    d = body(); chk_confirm(d); un = vstr(d, "username", UN_RX, 3, 24, "Username"); em = vstr(d, "email", EM_RX, 5, 254, "Email").lower()
    role = d.get("role", "user")
    if role not in ("user", "admin"): raise ApiError("Invalid role")
    try:
        uid = x("INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (un, em, generate_password_hash(vpass(d)), role, now())).lastrowid
    except sqlite3.IntegrityError: raise ApiError("Username or email is already in use", 409)
    log_act("admin.user.create", un); return {"id": uid, "message": "User created"}


def target_user(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u: raise ApiError("User not found", 404)
    return u


def admin_count(): return q("SELECT COUNT(*) c FROM users WHERE role='admin' AND suspended=0", one=True)["c"]


@api("/admin/users/<int:uid>", role="admin")
def a_user_get(uid):
    u = target_user(uid)
    return {"user": user_out(u), "sessions": q("SELECT COUNT(*) c FROM sessions WHERE user_id=?", (uid,), one=True)["c"],
            "vps": [vps_out(v, True) for v in q(VPS_SQL + " WHERE v.user_id=? ORDER BY v.id DESC", (uid,))],
            "activity": q("SELECT action,detail,ip,created_at FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 40", (uid,))}


@api("/admin/users/<int:uid>", ["PUT"], "admin")
def a_user_update(uid):
    u = target_user(uid); d = body(); sets, args = [], []
    if "email" in d: sets.append("email=?"); args.append(vstr(d, "email", EM_RX, 5, 254, "Email").lower())
    if "role" in d:
        if d["role"] not in ("user", "admin"): raise ApiError("Invalid role")
        if d["role"] != u["role"] and u["role"] == "admin" and admin_count() <= 1: raise ApiError("Cannot demote the last administrator")
        if uid == g.user["id"] and d["role"] != "admin": raise ApiError("You cannot demote yourself")
        sets.append("role=?"); args.append(d["role"])
    if d.get("password"): chk_confirm(d); sets.append("password_hash=?"); args.append(generate_password_hash(vpass(d)))
    if not sets: raise ApiError("Nothing to update")
    try: x(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*args, uid))
    except sqlite3.IntegrityError: raise ApiError("Email is already in use", 409)
    log_act("admin.user.update", u["username"]); return {"message": "User updated"}


@api("/admin/users/<int:uid>/sessions/revoke", ["POST"], "admin")
def a_user_signout(uid):
    u = target_user(uid); n = x("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount
    log_act("admin.user.signout", f"{u['username']}: {n} session(s)")
    return {"message": f"Signed {u['username']} out of {n} session(s)" if n else f"{u['username']} had no active sessions"}


@api("/admin/users/<int:uid>/<any(suspend,unsuspend):op>", ["POST"], "admin")
def a_user_suspend(uid, op):
    u = target_user(uid)
    if op == "suspend":
        if uid == g.user["id"]: raise ApiError("You cannot suspend yourself")
        if u["role"] == "admin" and admin_count() <= 1: raise ApiError("Cannot suspend the last administrator")
    x("UPDATE users SET suspended=? WHERE id=?", (1 if op == "suspend" else 0, uid))
    if op == "suspend":
        x("DELETE FROM sessions WHERE user_id=?", (uid,))
        for v in q("SELECT id FROM vps WHERE user_id=? AND status='running'", (uid,)): bg_stop(v["id"])
    log_act("admin.user." + op, u["username"]); return {"message": f"User {op}ed"}


@api("/admin/users/<int:uid>/reset-link", ["POST"], "admin")
def a_user_reset(uid):
    u = target_user(uid); tok = secrets.token_urlsafe(32)
    x("DELETE FROM reset_tokens WHERE user_id=? AND used=0", (uid,))
    x("INSERT INTO reset_tokens(user_id,token_hash,expires_at) VALUES(?,?,?)",
      (uid, hashlib.sha256(tok.encode()).hexdigest(), later(C.RESET_TTL)))
    log_act("admin.user.reset-link", u["username"]); return {"token": tok, "minutes": C.RESET_TTL}


@api("/admin/users/<int:uid>", ["DELETE"], "admin")
def a_user_delete(uid):
    u = target_user(uid); force = request.args.get("force") == "1"
    if uid == g.user["id"]: raise ApiError("You cannot delete yourself")
    if u["role"] == "admin" and admin_count() <= 1: raise ApiError("Cannot delete the last administrator")
    vs = q("SELECT * FROM vps WHERE user_id=?", (uid,))
    if vs and not force: raise ApiError(f"User owns {len(vs)} VPS. Delete them first or tick 'destroy their VPS'.", 409)
    for v in vs:
        destroy_container(node_of(v), v["name"]); x("DELETE FROM vps WHERE id=?", (v["id"],))
    x("DELETE FROM users WHERE id=?", (uid,)); log_act("admin.user.delete", u["username"])
    return {"message": "User deleted"}


# ---- admin: VPS
@api("/admin/vps", role="admin")
def a_vps(): return [vps_out(v, True) for v in q(VPS_SQL + " ORDER BY v.id DESC")]


@api("/admin/vps", ["POST"], "admin")
def a_vps_create():
    d = body(); owner = target_user(vint(d, "user_id", 1, 10**9, "Owner"))
    if owner["suspended"]: raise ApiError("Owner account is suspended")
    return create_vps(owner, d)


# ---- admin: nodes
def node_out(n):
    s = NODE_STATE.get(n["id"]); u = node_usage(n["id"])
    return {**{k: n[k] for k in ("id", "name", "host", "ssh_user", "ssh_port", "is_local", "enabled", "max_vps", "max_ram_mb",
                                 "max_cpu", "max_disk_gb", "lxc_path", "bridge", "created_at")},
            "online": None if s is None else s["online"], "error": s.get("error") if s else None,
            "stats": {k: s[k] for k in ("cpu", "cores", "ram_total", "ram_used", "disk_total", "disk_used", "load", "uptime")} if s and s["online"] else None,
            "vps_count": u["n"], "alloc": {"ram": u["ram"], "cpu": u["cpu"], "disk": u["disk"]}}


def node_fields(d):
    local = bool(d.get("is_local"))
    f = {"name": vstr(d, "name", NODE_RX, 1, 41, "Name"), "host": vstr(d, "host", HOST_RX, 1, 102, "Host"),
         "is_local": 1 if local else 0, "enabled": 1 if d.get("enabled", True) else 0,
         "ssh_user": vstr(d, "ssh_user", r"[a-z_][a-z0-9_-]{0,30}", 1, 31, "SSH user") if not local else "root",
         "ssh_port": vint(d, "ssh_port", 1, 65535, "SSH port") if not local else 22,
         "lxc_path": vstr(d, "lxc_path", PATH_RX, 1, 101, "Storage path"), "bridge": vstr(d, "bridge", BR_RX, 1, 15, "Bridge"),
         "max_vps": vint(d, "max_vps", 0, 100000, "Max VPS"), "max_ram_mb": vint(d, "max_ram_mb", 0, 10**9, "Max RAM"),
         "max_cpu": vint(d, "max_cpu", 0, 100000, "Max CPU"), "max_disk_gb": vint(d, "max_disk_gb", 0, 10**9, "Max disk")}
    if ".." in f["lxc_path"]: raise ApiError("LXC path is invalid")
    return f


@api("/admin/nodes", role="admin")
def a_nodes(): return [node_out(n) for n in q("SELECT * FROM nodes ORDER BY id")]


@api("/admin/nodes", ["POST"], "admin")
def a_node_create():
    f = node_fields(body())
    try:
        nid = x(f"INSERT INTO nodes({','.join(f)},created_at) VALUES({','.join('?' * len(f))},?)", (*f.values(), now())).lastrowid
    except sqlite3.IntegrityError: raise ApiError("A node with that name already exists", 409)
    log_act("admin.node.create", f["name"]); POOL.submit(lambda: refresh_node(q("SELECT * FROM nodes WHERE id=?", (nid,), one=True)))
    return {"id": nid, "message": "Node added"}


@api("/admin/nodes/<int:nid>", role="admin")
def a_node_get(nid):
    n = q("SELECT * FROM nodes WHERE id=?", (nid,), one=True)
    if not n: raise ApiError("Node not found", 404)
    return {"node": node_out(n), "vps": [vps_out(v, True) for v in q(VPS_SQL + " WHERE v.node_id=?", (nid,))],
            "history": list(HISTORY.get(nid, []))}


@api("/admin/nodes/<int:nid>", ["PUT"], "admin")
def a_node_update(nid):
    n = q("SELECT * FROM nodes WHERE id=?", (nid,), one=True)
    if not n: raise ApiError("Node not found", 404)
    d = body()
    if set(d) == {"enabled"}: f = {"enabled": 1 if d["enabled"] else 0}
    else: f = node_fields(d)
    try: x(f"UPDATE nodes SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), nid))
    except sqlite3.IntegrityError: raise ApiError("A node with that name already exists", 409)
    log_act("admin.node.update", n["name"]); POOL.submit(lambda: refresh_node(q("SELECT * FROM nodes WHERE id=?", (nid,), one=True)))
    return {"message": "Node updated"}


@api("/admin/nodes/<int:nid>", ["DELETE"], "admin")
def a_node_delete(nid):
    n = q("SELECT * FROM nodes WHERE id=?", (nid,), one=True)
    if not n: raise ApiError("Node not found", 404)
    if node_usage(nid)["n"]: raise ApiError("This node still hosts VPS instances. Move or delete them first.", 409)
    x("DELETE FROM nodes WHERE id=?", (nid,)); NODE_STATE.pop(nid, None); HISTORY.pop(nid, None)
    log_act("admin.node.delete", n["name"]); return {"message": "Node removed"}


@api("/admin/monitor", role="admin")
def a_monitor():
    return [{**node_out(n), "history": list(HISTORY.get(n["id"], []))} for n in q("SELECT * FROM nodes ORDER BY id")]


@api("/admin/activity", role="admin")
def a_activity():
    lim = min(max(int(request.args.get("limit", 200) or 200), 1), 500)
    return q("SELECT a.id,a.action,a.detail,a.ip,a.created_at,a.vps_id,u.username FROM activity a "
             "LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT ?", (lim,))


BOOL_KEYS = ("registration_enabled", "console_enabled", "discord_auth")


@api("/admin/settings", role="admin")
def a_settings_get():
    out = {k: setting(k) for k in DEFAULT_SETTINGS if k != "discord_client_secret"}
    out.update({"version": C.VERSION, "discord_client_secret_set": bool(setting("discord_client_secret")),
                "discord_callback": discord_redirect_uri()})
    return out


@api("/admin/settings", ["PUT"], "admin")
def a_settings_put():
    d = body(); vals = {}
    if "panel_name" in d: vals["panel_name"] = vstr(d, "panel_name", None, 1, 40, "Panel name")
    if "logo_text" in d: vals["logo_text"] = vstr(d, "logo_text", r"[A-Za-z0-9]{1,3}", 1, 3, "Logo letters").upper()
    if "accent" in d:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(d["accent"])): raise ApiError("Accent colour must look like #6d5efc")
        vals["accent"] = str(d["accent"]).lower()
    for k in BOOL_KEYS:
        if k in d: vals[k] = "1" if d[k] else "0"
    if "session_hours" in d: vals["session_hours"] = str(vint(d, "session_hours", 1, 720, "Session timeout"))
    if "max_sessions" in d: vals["max_sessions"] = str(vint(d, "max_sessions", 1, 100, "Max concurrent sessions"))
    if "site_description" in d: vals["site_description"] = vstr(d, "site_description", None, 1, 160, "Site description")
    if "panel_description" in d: vals["panel_description"] = vstr(d, "panel_description", None, 1, 120, "Panel description")
    if "panel_title" in d:
        t = str(d["panel_title"] or "").strip()
        if len(t) > 60 or re.search(r"[\x00-\x1f<>]", t): raise ApiError("Panel title is invalid (60 characters at most)")
        vals["panel_title"] = t
    if "site_icon" in d:
        icon = str(d["site_icon"] or "").strip()
        if icon and not (re.fullmatch(r"https://[^\s\"'<>\\]{4,480}", icon) or re.fullmatch(r"/[A-Za-z0-9_./%-]{1,200}", icon)):
            raise ApiError("Site icon must be an https:// URL or a path starting with /")
        vals["site_icon"] = icon
    if "discord_client_id" in d:
        cid = str(d["discord_client_id"] or "").strip()
        if cid and not re.fullmatch(r"\d{15,25}", cid): raise ApiError("The Discord client ID should be a long number")
        vals["discord_client_id"] = cid
    secret = str(d.get("discord_client_secret") or "").strip()
    if secret:                                                  # empty means: keep the saved secret
        if not re.fullmatch(r"[A-Za-z0-9_\-]{16,100}", secret): raise ApiError("That does not look like a Discord client secret")
        vals["discord_client_secret"] = secret
    if "discord_redirect" in d:
        red = str(d["discord_redirect"] or "").strip()
        if red and not re.fullmatch(r"https?://[^\s\"'<>\\]{4,300}", red): raise ApiError("Redirect URI must be a full http(s) URL")
        vals["discord_redirect"] = red
    if vals.get("discord_auth") == "1" and not (vals.get("discord_client_id", setting("discord_client_id")) and vals.get("discord_client_secret", setting("discord_client_secret"))):
        raise ApiError("Save the Discord client ID and client secret before turning Discord login on")
    if not vals: raise ApiError("Nothing to save")
    for k, v in vals.items(): x("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    apply_session_lifetime()
    log_act("admin.settings", ", ".join(k for k in vals if k != "discord_client_secret")); return {"message": "Settings saved"}


_LXD = {"t": 0.0, "v": ""}


def lxd_version():
    if time.time() - _LXD["t"] > 300:
        try: _LXD["v"] = subprocess.run(["lxc", "--version"], capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception: _LXD["v"] = ""
        _LXD["t"] = time.time()
    return _LXD["v"]


def _pkg(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception: return ""


@api("/admin/system", role="admin")
def a_system():
    try: dbs = os.path.getsize(C.DB)
    except OSError: dbs = 0
    cnt = lambda sql: q(sql, one=True)["c"]
    lv = lxd_version()
    return {"version": C.VERSION, "python": platform.python_version(), "os": platform.platform(), "db_name": os.path.basename(C.DB),
            "db_size": dbs, "uptime": int(time.time() - START), "threads": C.THREADS,
            "framework": ("Flask " + _pkg("flask") + " + Waitress").replace("  ", " "), "hypervisor": ("LXD " + lv) if lv else "LXD",
            "users": cnt("SELECT COUNT(*) c FROM users"), "vps": cnt("SELECT COUNT(*) c FROM vps"),
            "running": cnt("SELECT COUNT(*) c FROM vps WHERE status='running'"),
            "nodes": cnt("SELECT COUNT(*) c FROM nodes"), "nodes_online": sum(1 for st in NODE_STATE.values() if st.get("online")),
            "terminals": sum(1 for t in TERMS.values() if t.alive)}


@api("/admin/vps/stop-all", ["POST"], "admin")
def a_stop_all():
    """Emergency stop: every running VPS on every node."""
    n = 0
    for v in q("SELECT id FROM vps WHERE status='running'"):
        try: claim(v["id"], "stopping"); spawn(w_stop, v["id"], "stop"); n += 1
        except ApiError: pass
    log_act("admin.vps.stop-all", f"{n} running VPS")
    return {"message": f"Stopping {n} running VPS" if n else "No VPS is running", "count": n}


# ---- Discord sign-in (OAuth2 authorization code flow)
def discord_ready():
    return setting("discord_auth") == "1" and bool(setting("discord_client_id")) and bool(setting("discord_client_secret"))


def discord_redirect_uri():
    return setting("discord_redirect") or (request.host_url.rstrip("/") + "/auth/discord/callback")


def _discord_json(req):
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read().decode())
    except Exception as e:
        log.warning("discord oauth request failed: %s", e); raise ApiError("Discord sign-in failed", 502)


def discord_exchange(code, redirect):
    """Authorization code -> Discord profile (id, username, email, ...)."""
    ua = {"User-Agent": "FVMPanel/" + C.VERSION}
    data = urllib.parse.urlencode({"client_id": setting("discord_client_id"), "client_secret": setting("discord_client_secret"),
                                   "grant_type": "authorization_code", "code": code, "redirect_uri": redirect}).encode()
    tok = _discord_json(urllib.request.Request("https://discord.com/api/oauth2/token", data=data, method="POST",
                                               headers={**ua, "Content-Type": "application/x-www-form-urlencoded"}))
    if not tok.get("access_token"): raise ApiError("Discord sign-in failed", 502)
    me = _discord_json(urllib.request.Request("https://discord.com/api/users/@me", headers={**ua, "Authorization": "Bearer " + tok["access_token"]}))
    if not re.fullmatch(r"\d{5,25}", str(me.get("id", ""))): raise ApiError("Discord sign-in failed", 502)
    return me


def discord_new_user(me):
    base = re.sub(r"[^A-Za-z0-9_]", "", str(me.get("username") or ""))[:18] or "user"
    if len(base) < 3: base += "_dc"
    name, n = base, 1
    while q("SELECT 1 FROM users WHERE username=?", (name,), one=True): n += 1; name = f"{base}{n}"
    mail = str(me.get("email") or "").lower() if me.get("verified") else ""
    if not re.fullmatch(EM_RX, mail) or q("SELECT 1 FROM users WHERE email=?", (mail,), one=True): mail = f"discord-{me['id']}@discord.local"
    label = str(me.get("global_name") or me.get("username") or name)[:60]
    uid = x("INSERT INTO users(username,email,password_hash,created_at,discord_id,discord_name) VALUES(?,?,?,?,?,?)",
            (name, mail, "!", now(), str(me["id"]), label)).lastrowid
    return uid, name


@app.route("/auth/discord/start")
def discord_start():
    mode = "link" if request.args.get("mode") == "link" else "login"
    back = "/profile?discord_error=" if mode == "link" else "/login?error="
    if not discord_ready(): return redirect(back + "disabled")
    if mode == "link" and not g.user: return redirect("/login")
    state = secrets.token_urlsafe(24)
    session["dstate"], session["dmode"], session["dlink"] = state, mode, (g.user["id"] if mode == "link" else None)
    session.permanent = True
    qs = urllib.parse.urlencode({"client_id": setting("discord_client_id"), "response_type": "code", "redirect_uri": discord_redirect_uri(),
                                 "scope": "identify email", "state": state, "prompt": "none"})
    return redirect("https://discord.com/oauth2/authorize?" + qs)


@app.route("/auth/discord/callback")
def discord_callback():
    state, mode, link_uid = session.pop("dstate", ""), session.pop("dmode", "login"), session.pop("dlink", None)
    back = "/profile?discord_error=" if mode == "link" else "/login?error="
    try: throttle("dcb:" + str(request.remote_addr), 20, 300)
    except ApiError: return redirect(back + "busy")
    if not discord_ready(): return redirect(back + "disabled")
    if request.args.get("error"): return redirect(back + "denied")
    if not state or not secrets.compare_digest(request.args.get("state", "").encode(), state.encode()): return redirect(back + "state")
    try: me = discord_exchange(request.args.get("code", ""), discord_redirect_uri())
    except ApiError: return redirect(back + "failed")
    did, label = str(me["id"]), str(me.get("global_name") or me.get("username") or me["id"])[:60]
    owner = q("SELECT * FROM users WHERE discord_id=?", (did,), one=True)
    if mode == "link":
        if not g.user or g.user["id"] != link_uid: return redirect("/login")
        if owner and owner["id"] != g.user["id"]: return redirect(back + "taken")
        x("UPDATE users SET discord_id=?, discord_name=? WHERE id=?", (did, label, g.user["id"]))
        log_act("auth.discord.link", label); return redirect("/profile?discord=linked")
    if owner:
        if owner["suspended"]: return redirect(back + "suspended")
        x("UPDATE users SET discord_name=? WHERE id=?", (label, owner["id"]))
        login_user(owner["id"]); log_act("auth.discord.login", label, user_id=owner["id"]); return redirect("/overview")
    if setting("registration_enabled") != "1": return redirect(back + "nolink")
    uid, uname = discord_new_user(me); login_user(uid)
    log_act("auth.register", f"{uname} (discord)", user_id=uid)
    return redirect("/overview")


@api("/me/discord/unlink", ["POST"])
def me_discord_unlink():
    if g.user["password_hash"] == "!": raise ApiError("Set a password first, otherwise you could not sign in again.")
    x("UPDATE users SET discord_id=NULL, discord_name=NULL WHERE id=?", (g.user["id"],)); log_act("auth.discord.unlink")
    return {"message": "Discord disconnected"}


@app.route("/healthz")
def healthz(): return {"ok": True}


@app.route("/")
@app.route("/<path:p>")
def index(p=""):
    if p.startswith("api/") or p == "api": return jsonify({"error": "Not found"}), 404
    nonce = secrets.token_urlsafe(16)
    icon = setting("site_icon")
    fav = f'<link rel="icon" href="{escape(icon)}">' if icon else FAV_DEFAULT
    html = (PAGE.replace("__NONCE__", nonce).replace("__TITLE__", str(escape(setting("panel_title") or setting("panel_name"))))
            .replace("__DESC__", str(escape(setting("site_description")))).replace("__FAVICON__", fav))
    r = Response(html, mimetype="text/html")
    r.headers["Content-Security-Policy"] = (f"default-src 'none'; script-src 'nonce-{nonce}' {C.CDN}; style-src 'unsafe-inline' {C.CDN}; "
                                            "connect-src 'self'; img-src 'self' data: https:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return r


# ------------------------------------------------------------------ frontend (served from this file)
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__DESC__">
<meta name="author" content="Fahim">
<meta name="generator" content="FVM Panel">
__FAVICON__
<style>
:root{--bg:#000;--card:#111113;--card2:#17171a;--bd:#25252a;--tx:#ececf1;--mu:#8d8d98;--ac:#6d5efc;--ac2:#38bdf8;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--r:18px;color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--tx);font:14px/1.55 "Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
h1{font-size:26px;font-weight:650;letter-spacing:-.02em}h3{font-size:15px;font-weight:600;margin-bottom:14px}
.mu{color:var(--mu)}.row{display:flex;align-items:center}.sb{justify-content:space-between}.gap{gap:10px}.wrap{flex-wrap:wrap}.end{justify-content:flex-end}.around{justify-content:space-around}.grow{flex:1}
.ic{width:18px;height:18px;flex:none}
.app{display:flex;min-height:100vh}
.side{width:256px;background:#050506;border-right:1px solid var(--bd);padding:20px 14px;position:fixed;inset:0 auto 0 0;display:flex;flex-direction:column;z-index:30;transition:transform .3s}
.brand{display:flex;gap:12px;align-items:center;padding:4px 8px 22px}
.logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,var(--ac),var(--ac2));display:grid;place-items:center;font-weight:800;letter-spacing:-.04em}
.brand small{display:block;color:var(--mu);font-size:12px}
.side nav{display:flex;flex-direction:column;gap:3px}
.side nav a{display:flex;gap:12px;align-items:center;padding:10px 12px;border-radius:12px;color:var(--mu);cursor:pointer;transition:background .2s,color .2s}
.side nav a:hover{background:var(--card2);color:var(--tx)}
.side nav a.on{background:var(--card2);color:var(--tx);box-shadow:inset 3px 0 0 var(--ac)}
.side .sp2{flex:1}
.mainw{margin-left:256px;flex:1;min-width:0}
.top{height:62px;display:flex;align-items:center;gap:12px;padding:0 28px;border-bottom:1px solid var(--bd);position:sticky;top:0;background:rgba(0,0,0,.8);backdrop-filter:blur(12px);z-index:20}
#menu{display:none}
.chip{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--bd);padding:5px 14px 5px 5px;border-radius:99px}
.av{width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,var(--ac),var(--ac2));display:grid;place-items:center;font-weight:700;font-size:12px}
main{padding:28px;max-width:1280px}
.ph{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:20px;transition:border-color .25s,transform .25s}
.card:hover{border-color:#34343b}
.anim .card,.anim .ph{animation:rise .5s cubic-bezier(.2,.7,.2,1) both}.anim .card:nth-child(2){animation-delay:.05s}.anim .card:nth-child(3){animation-delay:.1s}.anim .card:nth-child(4){animation-delay:.15s}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.grid{display:grid;gap:16px;margin-bottom:16px}.g4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}.g3{grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}.g2{grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.stat{display:flex;gap:16px;align-items:center}.si{width:46px;height:46px;border-radius:14px;background:var(--card2);display:grid;place-items:center}.si .ic{width:22px;height:22px}.sv{font-size:26px;font-weight:650;line-height:1.1;letter-spacing:-.02em}
.btn{background:var(--card2);border:1px solid var(--bd);color:var(--tx);padding:9px 16px;border-radius:12px;font:inherit;font-weight:550;cursor:pointer;display:inline-flex;gap:8px;align-items:center;transition:transform .15s,background .2s,border-color .2s,opacity .2s}
.btn:hover{background:#202025;border-color:#3a3a42}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.4;pointer-events:none}
.btn.pri{background:linear-gradient(135deg,var(--ac),#5a8cf6);border-color:transparent}.btn.pri:hover{filter:brightness(1.12)}
.btn.danger{background:#2a1214;border-color:#4a1d21;color:#fca5a5}.btn.danger:hover{background:#3a1519}
.btn.ghost{background:transparent}.btn.sm{padding:5px 11px;font-size:13px;border-radius:10px}
.bd{display:inline-flex;gap:7px;align-items:center;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:600;background:var(--card2);border:1px solid var(--bd);margin-left:4px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--mu);display:inline-block}
.bd.running .dot{background:var(--ok);box-shadow:0 0 8px var(--ok)}.bd.running{color:var(--ok)}.bd.error .dot,.bd.suspended .dot{background:var(--bad)}.bd.error,.bd.suspended{color:var(--bad)}.bd.admin{color:var(--ac2)}
.bd.creating,.bd.starting,.bd.stopping,.bd.restarting,.bd.deleting,.bd.reinstalling{color:var(--warn)}
.sp{width:14px;height:14px;border:2px solid #333;border-top-color:var(--ac);border-radius:50%;display:inline-block;animation:spin .7s linear infinite}.sp.sm{width:10px;height:10px;border-width:2px;border-top-color:var(--warn)}
@keyframes spin{to{transform:rotate(360deg)}}
.bar{height:8px;background:#1d1d22;border-radius:9px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;border-radius:9px;background:linear-gradient(90deg,var(--ac),var(--ac2));transition:width .8s cubic-bezier(.2,.7,.2,1)}
.mt{margin-bottom:16px}
.kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #1b1b20}.kv:last-child{border:0}.kv span{color:var(--mu)}.kv b{font-weight:550;text-align:right;word-break:break-all}
.vc .ttl{font-size:17px;font-weight:600}.vc{display:flex;flex-direction:column;gap:12px}.vc:hover{transform:translateY(-3px)}
.specs{display:flex;gap:16px;color:var(--mu);flex-wrap:wrap}.specs span{display:flex;gap:6px;align-items:center}
.dn{text-align:center;color:var(--mu)}.dn canvas{display:block;margin:0 auto 6px}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--mu);font-weight:500;padding:10px 12px;border-bottom:1px solid var(--bd);white-space:nowrap}
td{padding:12px;border-bottom:1px solid #1b1b20;vertical-align:middle}tr:last-child td{border:0}tbody tr{transition:background .2s}tbody tr:hover{background:#15151a}
.tw{overflow-x:auto;padding:6px 8px}
.act{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid #1b1b20}.act:last-child{border:0}.act .dot{margin-top:7px;background:var(--ac)}.act b{font-weight:550}
.alert{background:#2a1214;border:1px solid #4a1d21;color:#fca5a5;border-radius:14px;padding:12px 16px;margin-bottom:16px}
.back{display:inline-block;color:var(--mu);margin-bottom:12px}.back:hover{color:var(--tx)}
.empty{text-align:center;padding:48px 20px;color:var(--mu)}.empty h3{color:var(--tx)}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}.fld label{color:var(--mu);font-size:13px}
input,select{background:#0a0a0c;border:1px solid var(--bd);color:var(--tx);border-radius:12px;padding:10px 13px;font:inherit;width:100%;transition:border-color .2s,box-shadow .2s}
input:focus,select:focus{outline:0;border-color:var(--ac);box-shadow:0 0 0 3px rgba(109,94,252,.2)}
.chk{display:flex;gap:10px;align-items:center;margin-bottom:14px;cursor:pointer}.chk input{width:auto;accent-color:var(--ac)}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.72);backdrop-filter:blur(6px);display:grid;place-items:center;z-index:50;padding:16px;animation:fade .2s}
.md{background:var(--card);border:1px solid #303036;border-radius:22px;padding:26px;width:100%;max-width:460px;max-height:92vh;overflow:auto;animation:pop .25s cubic-bezier(.2,.8,.2,1)}.md p{margin-bottom:14px}.md.wide{max-width:760px}
@keyframes fade{from{opacity:0}}@keyframes pop{from{opacity:0;transform:scale(.94) translateY(8px)}}
.term{background:#050506;border:1px solid var(--bd);border-radius:14px;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.term pre{height:340px;overflow:auto;padding:14px;white-space:pre-wrap;word-break:break-all;color:#cfd2dc}
.term form{display:flex;gap:8px;border-top:1px solid var(--bd);padding:8px 14px;align-items:center}.term input{border:0;background:transparent;padding:6px 0;box-shadow:none!important;font:inherit}
#toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:10px;z-index:90}
.toast{background:var(--card2);border:1px solid var(--bd);border-left:3px solid var(--ok);padding:12px 18px;border-radius:12px;min-width:240px;max-width:380px;box-shadow:0 12px 40px rgba(0,0,0,.6);animation:slide .3s cubic-bezier(.2,.8,.2,1)}
.toast.err{border-left-color:var(--bad)}.toast.out{opacity:0;transform:translateX(30px);transition:.3s}
@keyframes slide{from{opacity:0;transform:translateX(40px)}}
#ld{position:fixed;top:0;left:0;height:2px;width:0;background:linear-gradient(90deg,var(--ac),var(--ac2));z-index:99;transition:width .4s;opacity:0}#ld.on{opacity:1;width:80%;transition:width 8s cubic-bezier(.1,.8,.2,1)}
.auth{min-height:100vh;display:grid;place-items:center;padding:20px;background:radial-gradient(700px 360px at 50% -10%,rgba(109,94,252,.18),transparent)}
.auth .card{width:100%;max-width:410px;padding:32px;animation:pop .5s cubic-bezier(.2,.8,.2,1)}.auth .brand{justify-content:center;flex-direction:column;text-align:center;padding-bottom:24px}.auth .logo{width:54px;height:54px;font-size:20px;border-radius:16px}
.auth .btn{width:100%;justify-content:center;padding:12px}.auth p.sw{text-align:center;margin-top:16px;color:var(--mu)}.auth p.sw a{color:var(--tx);text-decoration:underline}
canvas.ln{width:100%;height:170px;display:block}.lg{display:flex;gap:16px;color:var(--mu);font-size:12px;margin-top:8px}.lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
@media(max-width:900px){.side{transform:translateX(-100%)}.side.open{transform:none;box-shadow:0 0 60px #000}.mainw{margin-left:0}#menu{display:inline-flex}main{padding:18px}.top{padding:0 16px}.g2{grid-template-columns:1fr}.chip span{display:none}}

/* ---- motion layer ---- */
.btn{position:relative;overflow:hidden}
.btn::after{content:'';position:absolute;inset:0;background:linear-gradient(110deg,transparent 30%,rgba(255,255,255,.14) 50%,transparent 70%);transform:translateX(-110%);pointer-events:none}
.btn:hover::after{transform:translateX(110%);transition:transform .7s}
.btn:hover{transform:translateY(-1px)}.btn:active{transform:scale(.96)}
.card:hover{box-shadow:0 10px 36px rgba(109,94,252,.09)}.vc:hover{transform:translateY(-4px)}
.bar i{transition:none}
.logo{box-shadow:0 0 26px rgba(109,94,252,.5);animation:glow 4s ease-in-out infinite}
@keyframes glow{50%{box-shadow:0 0 38px rgba(56,189,248,.55);transform:rotate(-3deg) scale(1.04)}}
.side nav a .ic{transition:transform .25s}.side nav a:hover{transform:translateX(4px)}.side nav a:hover .ic{transform:scale(1.18) rotate(-6deg)}
.live{display:inline-flex;gap:8px;align-items:center;color:var(--ok);font-size:12px;font-weight:600;background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.22);padding:3px 12px;border-radius:99px;visibility:hidden}
.live i{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 1.8s infinite}.live.tick{animation:flash .6s}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.6)}70%{box-shadow:0 0 0 9px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
@keyframes flash{30%{background:rgba(52,211,153,.28);transform:scale(1.06)}}
.bd.running .dot{animation:pulse 2s infinite}
.bd.creating,.bd.starting,.bd.stopping,.bd.restarting,.bd.deleting,.bd.reinstalling{animation:breathe 1.6s ease-in-out infinite}
@keyframes breathe{50%{opacity:.55}}
.anim .grid>.card:nth-child(1){animation-delay:0s}.anim .grid>.card:nth-child(2){animation-delay:.07s}.anim .grid>.card:nth-child(3){animation-delay:.14s}.anim .grid>.card:nth-child(4){animation-delay:.21s}.anim .grid>.card:nth-child(5){animation-delay:.28s}.anim .grid>.card:nth-child(6){animation-delay:.35s}
.anim tbody tr{animation:rise .45s both}.anim tbody tr:nth-child(1){animation-delay:.05s}.anim tbody tr:nth-child(2){animation-delay:.1s}.anim tbody tr:nth-child(3){animation-delay:.15s}.anim tbody tr:nth-child(4){animation-delay:.2s}.anim tbody tr:nth-child(5){animation-delay:.25s}.anim tbody tr:nth-child(n+6){animation-delay:.3s}
.anim .act{animation:rise .4s both}.anim .act:nth-child(2){animation-delay:.08s}.anim .act:nth-child(3){animation-delay:.16s}.anim .act:nth-child(n+4){animation-delay:.24s}
.sk{height:96px;background:linear-gradient(100deg,#111113 30%,#1b1b20 50%,#111113 70%);background-size:300% 100%;animation:shimmer 1.4s linear infinite;border-color:#1b1b20}
@keyframes shimmer{to{background-position:-300% 0}}
.auth{position:relative;overflow:hidden}.auth .card{position:relative;z-index:1}
.auth::before,.auth::after{content:'';position:absolute;width:440px;height:440px;border-radius:50%;filter:blur(100px);opacity:.3;animation:floaty 15s ease-in-out infinite alternate}
.auth::before{background:var(--ac);top:-120px;left:-100px}.auth::after{background:var(--ac2);bottom:-140px;right:-100px;animation-direction:alternate-reverse}
@keyframes floaty{to{transform:translate(90px,60px) scale(1.15)}}
.toast{position:relative;overflow:hidden}.toast::after{content:'';position:absolute;left:0;bottom:0;height:2px;width:100%;background:var(--ok);animation:tbar 4.5s linear forwards}.toast.err::after{background:var(--bad)}
@keyframes tbar{to{width:0}}
.dn canvas{filter:drop-shadow(0 0 10px rgba(109,94,252,.25))}
.empty h3{animation:rise .6s both}
.cg{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}.cg .sum{position:sticky;top:80px}
.osg{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}.osc input{display:none}
.osc span{display:flex;flex-direction:column;gap:2px;border:1px solid var(--bd);background:#0a0a0c;border-radius:14px;padding:14px;cursor:pointer;transition:transform .2s,border-color .2s,background .2s,box-shadow .2s}
.osc span:hover{transform:translateY(-2px);border-color:#3a3a42}.osc small{color:var(--mu)}
.osc input:checked+span{border-color:var(--ac);background:rgba(109,94,252,.12);box-shadow:0 0 0 3px rgba(109,94,252,.18)}
.sum .nm{font-size:22px;font-weight:650;letter-spacing:-.02em;word-break:break-all;margin-bottom:6px}
input[type=range]{accent-color:var(--ac);flex:1;min-width:0}
.cg .card h3 .n{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--ac);font-size:12px;margin-right:8px}
@media(max-width:900px){.cg{grid-template-columns:1fr}.cg .sum{position:static}}
/* ---- console page ---- */
.cn{display:flex;flex-direction:column;height:100vh;height:100dvh;background:var(--tbg,#050506)}
.cbar{display:flex;gap:12px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--bd);background:#09090b;flex-wrap:wrap;animation:rise .4s both}
.ctt{display:flex;gap:8px;align-items:center;font-size:15px}
.cwrap{position:relative;flex:1;min-height:0}
.cterm{position:absolute;inset:0;padding:10px 6px 6px 12px}.cterm .xterm{height:100%}.cterm .xterm-viewport{background:transparent!important}
.cov{position:absolute;inset:0;background:rgba(0,0,0,.74);backdrop-filter:blur(4px);display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;z-index:5;text-align:center;padding:20px;animation:fade .25s}
.cov.off{display:none}
.cst{display:inline-flex;gap:8px;align-items:center;font-size:12px;font-weight:600;padding:3px 12px;border-radius:99px;border:1px solid var(--bd);background:var(--card2)}
.cst i{width:7px;height:7px;border-radius:50%;background:var(--warn);animation:breathe 1.2s infinite}
.cst.ok{color:var(--ok)}.cst.ok i{background:var(--ok);animation:pulse 1.8s infinite}.cst.bad{color:var(--bad)}.cst.bad i{background:var(--bad);animation:none}.cst.c{color:var(--warn)}
.ck .btn{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;padding:5px 10px}
.cn select{width:auto;padding:5px 10px;border-radius:10px;font-size:13px}
.cn .bar-sep{width:1px;height:20px;background:var(--bd)}
.cf{width:100%;max-width:400px;background:var(--card);border:1px solid var(--bd);border-radius:20px;padding:24px;text-align:left;box-shadow:0 20px 60px rgba(0,0,0,.6);animation:pop .3s cubic-bezier(.2,.8,.2,1)}
.cf h3{display:flex;gap:10px;align-items:center;font-size:19px;margin-bottom:16px}.cf .btn{width:100%;justify-content:center;padding:11px}
.cf input[readonly]{opacity:.7;cursor:not-allowed}.cf .or{display:flex;align-items:center;gap:12px;color:var(--mu);font-size:12px;margin:14px 0}.cf .or::before,.cf .or::after{content:'';flex:1;height:1px;background:var(--bd)}
.vh{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:24px;animation:rise .45s both}
.vh .back{display:inline-flex;align-items:center;gap:4px;margin:0 10px 0 0;padding:6px 8px 6px 2px;border-radius:10px}.vh .back:hover{background:var(--card2)}
.vh h1{line-height:1.15}.vh .meta{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:6px}.vh .meta .bd{margin:0}
@media(max-width:900px){.vh>.row:last-child{width:100%}}
.ver{display:inline-flex;align-items:center;padding:8px 14px;border-radius:10px;background:var(--card2);border:1px solid var(--bd);font-size:12px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#d7d9e0;white-space:nowrap}
.bt{display:flex;flex-direction:column;align-items:flex-start;gap:5px;min-width:0}.bt b{font-size:16px;letter-spacing:-.01em;line-height:1.1}.bt .ver{padding:4px 10px;font-size:11px;border-radius:8px}
.vx{display:flex;flex-direction:column;gap:14px}.vx:hover{transform:translateY(-3px)}
.vxn .ttl{font-size:18px;font-weight:650;letter-spacing:-.01em}.vxn .mu{display:flex;gap:8px;align-items:center;margin-top:2px}.vxn .bd{margin:0;padding:1px 9px;font-size:11px}
.vx>.row>div>.bd{text-transform:uppercase;letter-spacing:.06em;font-size:11px}
.vxg{display:grid;grid-template-columns:1fr 1fr;gap:14px 18px;background:#0a0a0c;border:1px solid var(--bd);border-radius:14px;padding:14px}
.vxs{display:flex;gap:10px;align-items:center;min-width:0}.vxs small{display:block;color:var(--mu);font-size:11px;letter-spacing:.07em;text-transform:uppercase}.vxs b{display:block;font-weight:600;word-break:break-all}
.vxi{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;flex:none;color:var(--c);background:color-mix(in srgb,var(--c) 14%,transparent)}.vxi .ic{width:16px;height:16px}
.vxb .btn{flex:1;justify-content:center;padding:10px 8px}
.btn.ok{background:linear-gradient(135deg,#16a34a,#34d399);border-color:transparent;color:#03150b}.btn.ok:hover{filter:brightness(1.1);background:linear-gradient(135deg,#16a34a,#34d399)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--bd);margin-bottom:22px;overflow-x:auto}
.tab{display:flex;gap:9px;align-items:center;padding:12px 18px;color:var(--mu);border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;border-radius:10px 10px 0 0;transition:color .2s,border-color .2s,background .2s}
.tab:hover{color:var(--tx);background:var(--card2)}.tab.on{color:#fff;border-color:#fff;font-weight:600}
.tabc{max-width:660px;animation:rise .4s both}.tabc .row .logo{flex:none}
.tabc{max-width:760px}
.scard{padding:0;overflow:hidden}.scard .sh{display:flex;gap:12px;align-items:center;padding:18px 22px;border-bottom:1px solid var(--bd);font-size:15px}
.srow{display:grid;grid-template-columns:1fr minmax(160px,46%);gap:16px;align-items:center;padding:18px 22px;border-bottom:1px solid #1b1b20}
.srow>div b{display:block;font-weight:600}.srow>div small{color:var(--mu);font-size:12px}.sf{padding:18px 22px}
.sw{justify-self:end;position:relative;width:46px;height:26px;display:inline-block;cursor:pointer}
.sw input{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;padding:0;border:0;cursor:pointer;z-index:1}
.sw i{position:absolute;inset:0;background:#26262c;border-radius:99px;transition:background .25s}
.sw i::after{content:'';position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .25s}
.sw input:checked+i{background:var(--ac)}.sw input:checked+i::after{transform:translateX(20px)}.sw input:focus-visible+i{outline:2px solid var(--ac);outline-offset:2px}
@media(max-width:600px){.srow{grid-template-columns:1fr}.sw{justify-self:start}}
.logo img{width:100%;height:100%;object-fit:cover;border-radius:inherit;display:block}.logo{overflow:hidden}
.btn.dc{background:#5865f2;border-color:transparent;color:#fff}.btn.dc:hover{background:#6a76f5;border-color:transparent}
.or{display:flex;align-items:center;gap:12px;color:var(--mu);margin:16px 0;font-size:12px}.or::before,.or::after{content:'';flex:1;height:1px;background:var(--bd)}
.srow .row input[readonly]{opacity:.85}
.merr{background:#2a1214;border:1px solid #4a1d21;color:#fca5a5;border-radius:12px;padding:10px 14px;margin:0 0 14px;animation:rise .25s both}
.sysg{display:grid;grid-template-columns:1fr 1fr;gap:0 44px;padding:18px 22px 10px}.sysg .kv{padding:11px 0}
.sec{color:var(--mu);font-size:11px;letter-spacing:.12em;text-transform:uppercase;margin-bottom:4px}.okc{color:var(--ok);display:inline-flex;align-items:center}
.dz{border-color:#3a1a1d}.dzh{color:#ef4444}.dzh .ic{color:#ef4444}
.dzb{border-color:#ef4444;color:#fecaca;background:#1a0b0d;box-shadow:0 0 14px rgba(239,68,68,.3);justify-self:end}.dzb:hover{background:#2a1214;border-color:#ef4444;box-shadow:0 0 24px rgba(239,68,68,.55)}
@media(max-width:700px){.sysg{grid-template-columns:1fr}}
/* ---- sign-in pages ---- */
.auth2{min-height:100vh;display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr)}
.hero{position:relative;overflow:hidden;padding:40px 52px;display:flex;flex-direction:column;justify-content:space-between;border-right:1px solid var(--bd);
 background:radial-gradient(720px 440px at 12% 8%,color-mix(in srgb,var(--ac) 30%,transparent),transparent),radial-gradient(620px 420px at 95% 100%,color-mix(in srgb,var(--ac2) 20%,transparent),transparent),#030304}
.hero::before{content:'';position:absolute;inset:0;background-image:linear-gradient(#ffffff0a 1px,transparent 1px),linear-gradient(90deg,#ffffff0a 1px,transparent 1px);background-size:44px 44px;-webkit-mask-image:radial-gradient(circle at 30% 30%,#000,transparent 72%);mask-image:radial-gradient(circle at 30% 30%,#000,transparent 72%);animation:gridmove 20s linear infinite}
@keyframes gridmove{to{background-position:44px 44px}}
.hero>*{position:relative}.hb{display:flex;gap:14px;align-items:center;font-size:20px}.hb .logo{width:46px;height:46px;border-radius:14px}
.hm h2{font-size:40px;line-height:1.1;letter-spacing:-.03em;max-width:470px;font-weight:700;animation:rise .7s .1s both}
.hm p{color:var(--mu);margin-top:16px;max-width:450px;font-size:15px;animation:rise .7s .22s both}.hero .ver{align-self:flex-start}
.apane{display:grid;place-items:center;padding:32px 20px}.acard{width:100%;max-width:410px;animation:pop .5s cubic-bezier(.2,.8,.2,1)}
.acard h1{font-size:28px;margin-bottom:6px}.asub{margin-bottom:22px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:4px;background:var(--card);border:1px solid var(--bd);padding:4px;border-radius:14px;margin-bottom:28px}
.seg a{text-align:center;padding:9px;border-radius:10px;color:var(--mu);font-weight:550;transition:background .2s,color .2s}.seg a.on{background:var(--card2);color:#fff}.seg a:hover:not(.on){color:var(--tx)}
.iw{position:relative}.iw>.ic{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--mu);pointer-events:none;transition:color .2s}.iw input{padding-left:44px;padding-top:12px;padding-bottom:12px}.iw:focus-within>.ic{color:var(--ac)}
.iw.pw input{padding-right:48px}
.eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:none;border:0;color:var(--mu);width:36px;height:36px;border-radius:10px;cursor:pointer;display:grid;place-items:center;transition:background .2s,color .2s}.eye:hover{color:var(--tx);background:var(--card2)}
.aerr{background:#2a1214;border:1px solid #4a1d21;color:#fca5a5;border-radius:12px;padding:10px 14px;margin-bottom:14px;animation:rise .25s both}.aerr[hidden]{display:none}
.btn.big{width:100%;justify-content:center;padding:13px;font-size:15px}
.alinks{text-align:center;margin-top:20px;color:var(--mu)}.alinks a{color:var(--tx)}.alinks a:hover{text-decoration:underline}
.caps{color:var(--warn);font-size:12px;margin:-6px 0 12px}.caps[hidden]{display:none}
.pwm{height:4px;background:#1d1d22;border-radius:9px;overflow:hidden;margin:-6px 0 6px}.pwm i{display:block;height:100%;width:0;border-radius:9px;transition:width .3s,background .3s}
.pwl{font-size:12px;color:var(--mu);margin-bottom:12px;min-height:16px}
.acard.shake{animation:shake .4s}@keyframes shake{20%,60%{transform:translateX(-7px)}40%,80%{transform:translateX(7px)}}
@media(max-width:900px){.auth2{grid-template-columns:1fr}.hero{padding:20px;border-right:0;border-bottom:1px solid var(--bd)}.hm,.hero .ver{display:none}.apane{padding:26px 18px 40px}}
tr[data-href]{cursor:pointer}
.sov{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;padding:18px 22px 22px}
.tile{border:1px solid;border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:10px;transition:transform .2s}.tile:hover{transform:translateY(-3px)}
.tile b{font-size:14px}.tile small{color:var(--mu);margin-top:-6px}.tile .ic{width:22px;height:22px}
.t-ok{border-color:#14532d;background:rgba(34,197,94,.07);color:#4ade80}.t-bl{border-color:#312e81;background:rgba(99,102,241,.08);color:#a5b4fc}
.t-or{border-color:#7c2d12;background:rgba(249,115,22,.08);color:#fb923c}.t-pu{border-color:#581c87;background:rgba(168,85,247,.08);color:#d8b4fe}
/* ---- notifications + credit ---- */
.bell{position:relative;padding:8px 10px}.bcount{position:absolute;top:-5px;right:-5px;min-width:18px;height:18px;padding:0 5px;border-radius:99px;background:#ef4444;color:#fff;font:700 11px/18px system-ui;text-align:center;font-style:normal;box-shadow:0 0 0 2px #000;animation:pop .3s}.bcount[hidden]{display:none}
.bell.ring .ic{animation:ring .8s}@keyframes ring{10%,30%,50%,70%{transform:rotate(14deg)}20%,40%,60%,80%{transform:rotate(-14deg)}100%{transform:none}}
.np{position:fixed;top:66px;right:20px;width:400px;max-width:calc(100vw - 24px);background:var(--card);border:1px solid #303036;border-radius:18px;box-shadow:0 24px 60px rgba(0,0,0,.65);z-index:45;overflow:hidden;animation:pop .22s cubic-bezier(.2,.8,.2,1)}
.nph{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--bd)}
.npl{max-height:min(60vh,440px);overflow:auto}.npe{padding:34px 16px;text-align:center;color:var(--mu)}
.npf{display:block;text-align:center;padding:12px;border-top:1px solid var(--bd);color:var(--mu);font-weight:550}.npf:hover{color:var(--tx);background:var(--card2)}
.ni{display:flex;gap:12px;align-items:flex-start;padding:12px 16px;border-bottom:1px solid #1b1b20;transition:background .2s}.ni:hover{background:#15151a}.ni:last-child{border:0}
.ni.un{background:rgba(109,94,252,.07)}.nb{flex:1;min-width:0}.nt{word-break:break-word}.nb small{color:var(--mu)}
.nd{width:8px;height:8px;border-radius:50%;background:var(--ac);margin-top:7px;flex:none}
.nic{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;flex:none;background:var(--card2);color:var(--mu)}.nic .ic{width:17px;height:17px}
.lv-ok{color:var(--ok);background:rgba(52,211,153,.1)}.lv-err{color:var(--bad);background:rgba(248,113,113,.1)}.lv-warn{color:var(--warn);background:rgba(251,191,36,.1)}
.nlist{padding:0;overflow:hidden}.nlist .ni{padding:14px 20px}
.mainw{display:flex;flex-direction:column}.mainw main{flex:1;width:100%}.foot{padding:22px 28px 28px;text-align:center}
.credit{display:inline-flex;gap:6px;align-items:baseline;color:var(--mu);font-size:12px;letter-spacing:.02em}.credit b{color:var(--tx);font-weight:650}
.credit em{font-style:normal;font-weight:650;background:linear-gradient(90deg,var(--ac),var(--ac2));-webkit-background-clip:text;background-clip:text;color:transparent}
.scredit{padding:14px 8px 0;border-top:1px solid #15151a;margin-top:12px;text-align:center}
.hf{display:flex;flex-direction:column;align-items:flex-start;gap:14px}
.awrap{width:100%;max-width:410px}.acredit{display:none;text-align:center;margin-top:28px}
@media(max-width:900px){.hf{display:none}.acredit{display:block}.np{right:12px;top:62px}}
.wm{position:fixed;right:16px;bottom:11px;z-index:3;pointer-events:none;user-select:none;-webkit-user-select:none;font:700 11px/1 system-ui,sans-serif;letter-spacing:.2em;color:rgba(255,255,255,.17)}.wm i{font-style:normal;margin:0 7px;opacity:.7}
.ni{padding:0;align-items:stretch;gap:0}.nlist .ni{padding:0}
.nlk{display:flex;gap:12px;align-items:flex-start;flex:1;min-width:0;padding:12px 0 12px 16px}.nlist .nlk{padding:14px 0 14px 20px}
.nx{align-self:center;background:none;border:0;color:var(--mu);width:34px;height:34px;margin:0 8px;border-radius:10px;cursor:pointer;display:grid;place-items:center;opacity:0;transition:opacity .2s,background .2s,color .2s}
.ni:hover .nx,.nx:focus-visible{opacity:1}.nx:hover{background:var(--card2);color:var(--tx)}@media(hover:none){.nx{opacity:.55}}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.chip2{padding:7px 15px;border-radius:99px;border:1px solid var(--bd);color:var(--mu);background:var(--card);transition:color .2s,background .2s,border-color .2s}
.chip2:hover{color:var(--tx)}.chip2.on{background:var(--card2);color:#fff;border-color:#3a3a42}
.nday{padding:9px 20px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mu);background:#0c0c0e;border-bottom:1px solid #1b1b20}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
</style></head>
<!-- FVM Panel (c) 2026 Fahim -->
<body><div id="app"></div><div class="wm" aria-hidden="true">FVM PANEL<i>•</i>© Fahim</div><div id="modal"></div><div id="toasts"></div><div id="ld"></div>
<script nonce="__NONCE__">
(()=>{'use strict';
const NONCE=(document.currentScript&&document.currentScript.nonce)||'';
const $=(s,e=document)=>e.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);
const IC={grid:'<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
server:'<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
sliders:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
out:'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
users:'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
cpu:'<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>',
pulse:'<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',shield:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
back:'<path d="M19 12H5M12 19l-7-7 7-7"/>',x:'<path d="M18 6L6 18M6 6l12 12"/>',bell:'<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',key:'<circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3L21 2M17 6l3 3M14 9l2 2"/>',mail:'<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 6l-10 7L2 6"/>',eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',eyeoff:'<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/>',warn:'<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',bolt:'<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>',clock:'<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',pen:'<path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/><path d="M2 2l7.6 7.6"/><circle cx="11" cy="11" r="2"/>',chat:'<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z"/>',info:'<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',chev:'<path d="M15 18l-6-6 6-6"/>',db:'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',disk:'<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>',globe:'<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',menu:'<path d="M3 6h18M3 12h18M3 18h18"/>',
mon:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
log:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
box:'<path d="M21 16V8l-9-5-9 5v8l9 5z"/><path d="M3.3 7 12 12l8.7-5M12 22V12"/>',term:'<path d="M4 17l6-6-6-6M12 19h8"/>'};
const ic=n=>`<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${IC[n]||''}</svg>`;
let CSRF='',ME=null,CFG={},timer=null,TOK=0,CUR=null,shellFor=null,BUSY=0;
const CH={},A={},F={},app=$('#app');
const TR=['creating','starting','stopping','restarting','deleting','reinstalling'];
const fb=b=>{if(b==null)return '—';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]};
const fu=s=>{if(!s)return '—';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'d ':'')+(h||d?h+'h ':'')+m+'m'};
const fd=i=>i?new Date(i).toLocaleString():'—';
const pc=(a,b)=>b?Math.max(0,Math.min(100,Math.round(a/b*100))):0;
const col=p=>p>85?'#f87171':p>65?'#fbbf24':'#34d399';
function toast(m,t){const e=document.createElement('div');e.className='toast '+(t||'');e.textContent=m;$('#toasts').appendChild(e);setTimeout(()=>{e.classList.add('out');setTimeout(()=>e.remove(),300)},4500)}
function ld(on){BUSY=Math.max(0,BUSY+(on?1:-1));const l=$('#ld');if(BUSY>0)l.classList.add('on');else{l.classList.remove('on');l.style.width='100%';setTimeout(()=>{l.style.width=''},350)}}
async function api(p,m,b){const o={method:m||'GET',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},credentials:'same-origin'};if(b!==undefined)o.body=JSON.stringify(b);
 let r;try{r=await fetch('/api'+p,o)}catch(e){throw new Error('Cannot reach the panel. Check your connection.')}
 let d={};try{d=await r.json()}catch(e){}
 if(r.status===401&&ME){ME=null;toast('Your session expired. Sign in again.','err');nav('/login',1);throw new Error(d.error||'Session expired')}
 if(!r.ok)throw new Error(d.error||'Request failed ('+r.status+')');if(m&&m!=='GET'&&!p.startsWith('/notifications')&&ME)kickBell();return d}
async function go(fn,msg){ld(1);try{const r=await fn();if(msg)toast(typeof msg==='function'?msg(r):msg);if(CUR)await CUR();return r}catch(e){toast(e.message,'err')}finally{ld(0)}}
function closeModal(){$('#modal').innerHTML=''}
function rawModal(h,wide){$('#modal').innerHTML=`<div class="ov"><div class="md${wide?' wide':''}">${h}</div></div>`;const ov=$('#modal .ov');ov.addEventListener('mousedown',e=>{if(e.target===ov)closeModal()})}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();const b=$('#bellp');if(b)b.remove()}});
function ask(title,fields,ok,danger,note,submit){fields=fields||[];return new Promise(res=>{
 const fh=fields.map(f=>{const id='f_'+f.name,rq=f.required===false?'':'required';
  if(f.type==='checkbox')return `<label class="chk"><input type="checkbox" id="${id}" name="${f.name}"${f.value?' checked':''}><span>${esc(f.label)}</span></label>`;
  let inp;if(f.type==='select')inp=`<select id="${id}" name="${f.name}">${f.options.map(o=>`<option value="${esc(o[0])}"${String(o[0])===String(f.value)?' selected':''}>${esc(o[1])}</option>`).join('')}</select>`;
  else inp=`<input id="${id}" name="${f.name}" type="${f.type||'text'}" value="${esc(f.value==null?'':f.value)}" placeholder="${esc(f.placeholder||'')}" ${rq} autocomplete="off"${f.min!=null?(f.type==='password'?` minlength="${f.min}"`:` min="${f.min}"`):''}${f.max!=null?` max="${f.max}"`:''}>`;
  return `<div class="fld"><label for="${id}">${esc(f.label)}</label>${inp}</div>`}).join('');
 $('#modal').innerHTML=`<div class="ov"><form class="md"><h3>${esc(title)}</h3>${note?`<p class="mu">${esc(note)}</p>`:''}${fh}<div class="merr" id="merr" hidden></div><div class="row end gap"><button type="button" class="btn ghost" data-x="1">Cancel</button><button class="btn ${danger?'danger':'pri'}" data-ok="1">${esc(ok||'Confirm')}</button></div></form></div>`;
 const ov=$('#modal .ov'),fm=$('#modal form'),err=fm.querySelector('#merr'),okb=fm.querySelector('[data-ok]'),cb=fm.querySelector('[data-x]'),done=v=>{closeModal();res(v)};
 if(!submit&&!fields.length)ov.addEventListener('mousedown',e=>{if(e.target===ov)done(null)});   // forms never close by an accidental click outside
 cb.onclick=()=>done(null);
 fm.addEventListener('submit',async e=>{e.preventDefault();if(okb.disabled)return;const v={};fields.forEach(f=>{const el=fm.elements[f.name];v[f.name]=f.type==='checkbox'?el.checked:f.type==='number'?Number(el.value):el.value});
  if(!submit){done(v);return}
  err.hidden=true;okb.disabled=true;cb.disabled=true;okb.innerHTML='<i class="sp"></i>';
  try{await submit(v);done(true)}                                                                    // success: close
  catch(x){err.textContent=(x&&x.message)||'Something went wrong';err.hidden=false;okb.disabled=false;cb.disabled=false;okb.textContent='Try again'}});   // error: stay open
 const fi=fm.querySelector('input:not([type=checkbox]),select');if(fi)fi.focus()})}
// ---------- charts
function donut(c,p,color){const d=devicePixelRatio||1,s=c.clientWidth||110;c.width=c.height=s*d;const g=c.getContext('2d');g.scale(d,d);const r=s/2-9,m=s/2;g.lineWidth=9;g.lineCap='round';g.strokeStyle='#202026';g.beginPath();g.arc(m,m,r,0,7);g.stroke();p=Math.max(0,Math.min(100,p||0));if(p>0){g.strokeStyle=color;g.beginPath();g.arc(m,m,r,-Math.PI/2,-Math.PI/2+p/100*Math.PI*2);g.stroke()}g.fillStyle='#ececf1';g.font='600 '+Math.round(s*.21)+'px system-ui';g.textAlign='center';g.textBaseline='middle';g.fillText(Math.round(p)+'%',m,m)}
function line(c,series,r){const d=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*d;c.height=h*d;const g=c.getContext('2d');g.scale(d,d);if(r!=null&&r<1){g.beginPath();g.rect(0,0,w*r,h);g.clip()}g.strokeStyle='#1f1f25';g.lineWidth=1;for(let i=0;i<=4;i++){const y=8+(h-16)*i/4;g.beginPath();g.moveTo(0,y);g.lineTo(w,y);g.stroke()}
 series.forEach(s=>{const n=s.data.length;if(n<2)return;const pts=s.data.map((v,i)=>[i/(n-1)*w,8+(h-16)*(1-Math.min(100,v||0)/100)]);g.beginPath();pts.forEach((p,i)=>i?g.lineTo(p[0],p[1]):g.moveTo(p[0],p[1]));g.strokeStyle=s.color;g.lineWidth=2;g.lineJoin='round';g.stroke();g.lineTo(w,h);g.lineTo(0,h);g.closePath();const gr=g.createLinearGradient(0,0,0,h);gr.addColorStop(0,s.color+'30');gr.addColorStop(1,s.color+'00');g.fillStyle=gr;g.fill()})}
const PREV={},RM=matchMedia('(prefers-reduced-motion: reduce)').matches,ease=t=>1-Math.pow(1-t,3);
function tween(from,to,ms,fn){if(RM||from===to){fn(to);return}const t0=performance.now();(function f(t){const k=Math.min(1,Math.max(0,(t-t0)/ms));fn(from+(to-from)*ease(k));if(k<1)requestAnimationFrame(f)})(t0)}
function paint(){const base=location.pathname;let i=0;
 document.querySelectorAll('canvas[data-donut]').forEach(c=>{const k=base+'d'+(i++),to=+c.dataset.donut,from=PREV[k]==null?0:PREV[k];PREV[k]=to;tween(from,to,950,v=>donut(c,v,col(v)))});
 i=0;document.querySelectorAll('.bar i[data-w]').forEach(e=>{const k=base+'b'+(i++),to=+e.dataset.w,from=PREV[k]==null?0:PREV[k];PREV[k]=to;tween(from,to,850,v=>{e.style.width=v+'%'})});
 i=0;document.querySelectorAll('[data-count]').forEach(e=>{const k=base+'n'+(i++),to=+e.dataset.count,from=PREV[k]==null?0:PREV[k];PREV[k]=to;tween(from,to,800,v=>{e.textContent=Math.round(v)})});
 i=0;document.querySelectorAll('canvas[data-line]').forEach(c=>{const k=base+'l'+(i++),sr=CH[c.dataset.line]||[];if(PREV[k]){line(c,sr);return}PREV[k]=1;tween(0,1,1100,v=>line(c,sr,v))})}
function tick(){const l=$('#live');if(l){l.classList.remove('tick');void l.offsetWidth;l.classList.add('tick')}}
const SKEL='<div class="grid g4">'+'<div class="card sk"></div>'.repeat(4)+'</div><div class="grid g2"><div class="card sk" style="height:220px"></div><div class="card sk" style="height:220px"></div></div>';
addEventListener('resize',()=>paint());
// ---------- view helpers
const head=(t,s,a)=>`<div class="ph"><div><h1>${esc(t)}</h1>${s?`<p class="mu">${s}</p>`:''}</div><div class="row gap wrap">${a||''}</div></div>`;
const stat=(l,v,i,c)=>`<div class="card stat"><div class="si" style="color:${c||'var(--ac)'}">${ic(i)}</div><div><div class="mu">${l}</div><div class="sv"${typeof v==='number'?` data-count="${v}"`:''}>${typeof v==='number'?0:v}</div></div></div>`;
const badge=s=>`<span class="bd ${s}">${TR.includes(s)?'<i class="sp sm"></i>':'<i class="dot"></i>'}${esc(s)}</span>`;
const vb=v=>badge(v.status)+(v.suspended?badge('suspended'):'');
const kv=(k,v)=>`<div class="kv"><span>${k}</span><b>${v}</b></div>`;
const dn=(p,l)=>`<div class="dn"><canvas data-donut="${p}" style="width:104px;height:104px"></canvas>${l}</div>`;
const meter=(l,a,b,f)=>{const p=pc(a,b);return `<div class="mt"><div class="row sb"><span>${l}</span><span class="mu">${f(a)} / ${f(b)}</span></div><div class="bar"><i data-w="${p}" style="width:0;background:${col(p)}"></i></div></div>`};
const actItem=a=>`<div class="act"><i class="dot"></i><div><b>${esc(a.action)}</b> ${esc(a.detail||'')}<div class="mu">${a.username?esc(a.username)+' · ':''}${fd(a.created_at)}</div></div></div>`;
const osName=v=>((CFG.os_names&&CFG.os_names[v.os])||cap(v.os))+' '+v.os_version;
const vcard=v=>{const run=v.status==='running',busy=TR.includes(v.status),
 cell=(i,c,l,val)=>`<div class="vxs"><span class="vxi" style="--c:${c}">${ic(i)}</span><div><small>${l}</small><b>${val}</b></div></div>`;
 return `<div class="card vx"><div class="row sb gap"><div class="vxn"><a class="ttl" href="/vps/${v.id}">${esc(v.label||v.name)}</a><div class="mu">${esc(osName(v))} <span class="bd">#${v.id}</span></div></div><div>${vb(v)}</div></div>
 <div class="vxg">${cell('db','#34d399','Memory',v.ram_mb+' MB')}${cell('cpu','#f472b6','vCPUs',v.cpu+(v.cpu===1?' Core':' Cores'))}${cell('disk','#38bdf8','Storage',v.disk_gb+'G')}${cell('globe','#8b7cff','Network',v.ip?esc(v.ip):'No IP yet')}</div>
 <div class="row gap vxb">${run?`<button class="btn" data-act="vps-op" data-op="stop" data-id="${v.id}">Stop</button>`:`<button class="btn ok"${busy||v.suspended?' disabled':''} data-act="vps-op" data-op="start" data-id="${v.id}">Start</button>`}<a class="btn pri" href="/vps/${v.id}">Details</a>${run&&!v.suspended?`<a class="btn" href="/vps/${v.id}/console" target="_blank" rel="noopener">${ic('term')}Console</a>`:`<button class="btn" disabled>${ic('term')}Console</button>`}</div></div>`};
const empty=(t,s,b)=>`<div class="card empty"><h3>${t}</h3><p>${s}</p>${b||''}</div>`;
const osOpts=()=>CFG.os.map(o=>[o.id,o.label]);
function creditHTML(){return '<div class="credit"><b>FVM Panel</b> <span>© 2026</span> <em>Fahim</em></div>'}
const NIC={vps:'server',auth:'user',admin:'shield',info:'info'};
const ago=i=>{const s=Math.max(0,(Date.now()-new Date(i).getTime())/1000);if(s<45)return 'just now';if(s<3600)return Math.round(s/60)+'m ago';if(s<86400)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago'};
const nbase=()=>location.pathname.startsWith('/admin')?'/admin':'';
const ntext=n=>{const who=n.actor?(n.actor_id===ME.id?'You':n.actor):'';return who?`<b>${esc(who)}</b> ${esc(n.text)}`:esc(cap(n.text))};
const nplain=n=>n.actor?(n.actor_id===ME.id?'You':n.actor)+' '+n.text:cap(n.text);
const nlink=n=>n.vps_id&&!/deleted/.test(n.text)?nbase()+'/vps/'+n.vps_id:'';
const dayLabel=i=>{const d=new Date(i),same=(a,b)=>a.toDateString()===b.toDateString();return same(d,new Date())?'Today':same(d,new Date(Date.now()-864e5))?'Yesterday':d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})};
const nrow=(n,compact)=>`<div class="ni${n.read?'':' un'}"><a class="nlk" href="${nlink(n)||'#'}" data-act="notif-open" data-id="${n.id}" data-href="${nlink(n)}"><span class="nic lv-${n.level}">${ic(n.level==='err'?'warn':n.level==='ok'?'pulse':NIC[n.kind]||'info')}</span><div class="nb"><div class="nt">${ntext(n)}</div><small>${compact?ago(n.created_at):ago(n.created_at)+' · '+fd(n.created_at)}</small></div>${n.read?'':'<i class="nd"></i>'}</a><button class="nx" data-act="notif-del" data-id="${n.id}" aria-label="Dismiss" title="Dismiss">${ic('x')}</button></div>`;
let bellTimer=null,bellN=null,bellId=null,bellKick=null,NF='all';
const ntoastOn=()=>lsGet('fvm_ntoast')!=='0';
function setBell(n){const c=$('#bcount'),b=$('#bell');if(!c)return;c.textContent=n>99?'99+':n;c.hidden=!n;if(bellN!==null&&n>bellN&&b){b.classList.remove('ring');void b.offsetWidth;b.classList.add('ring')}bellN=n}
async function refreshBell(){if(!ME)return;try{const r=await api('/notifications/count');setBell(r.unread);
 if(bellId!==null&&r.latest>bellId&&ntoastOn()){const f=await api('/notifications?since='+bellId+'&limit=5'),res=f.items.filter(n=>n.level==='ok'||n.level==='err').reverse();   // pop up results and failures; requests were already acknowledged
  res.slice(0,3).forEach(n=>toast(nplain(n),n.level==='err'?'err':''));if(res.length>3)toast('+'+(res.length-3)+' more notifications')}
 bellId=r.latest}catch(e){}}
function kickBell(){clearTimeout(bellKick);bellKick=setTimeout(refreshBell,700)}
function startBell(){refreshBell();if(!bellTimer)bellTimer=setInterval(()=>{if(ME&&!document.hidden)refreshBell()},8000)}
function stopBell(){clearInterval(bellTimer);bellTimer=null;bellN=null;bellId=null;const p=$('#bellp');if(p)p.remove()}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&ME&&bellTimer)refreshBell()});
async function bellPanel(){const r=await api('/notifications?limit=12');let p=$('#bellp');if(!p){p=document.createElement('div');p.id='bellp';p.className='np';document.body.appendChild(p)}
 p.innerHTML=`<div class="nph"><b>Notifications</b><div class="row gap"><button class="btn sm ghost" data-act="notif-prefs" title="Notification settings" aria-label="Notification settings">${ic('sliders')}</button>${r.unread?'<button class="btn sm ghost" data-act="notif-readall">Mark all read</button>':''}</div></div><div class="npl">${r.items.length?r.items.map(n=>nrow(n,true)).join(''):'<div class="npe">No notifications yet</div>'}</div><a class="npf" href="${nbase()}/notifications" data-act="notif-all" data-href="${nbase()}/notifications">View all notifications</a>`;setBell(r.unread)}
const NFS=[['all','All'],['unread','Unread'],['vps','VPS'],['auth','Account']];
async function vNotifs(){const tabs=NFS.concat(ME.role==='admin'?[['admin','Admin']]:[]),qs=NF==='unread'?'&unread=1':['vps','auth','admin'].includes(NF)?'&kind='+NF:'';
 const r=await api('/notifications?limit=200'+qs);setBell(r.unread);
 const chips=`<div class="chips">${tabs.map(t=>`<a href="#" class="chip2${NF===t[0]?' on':''}" data-act="notif-filter" data-f="${t[0]}">${t[1]}</a>`).join('')}</div>`;
 let last='';const list=r.items.map(n=>{const d=dayLabel(n.created_at),h=d!==last?`<div class="nday">${esc(d)}</div>`:'';last=d;return h+nrow(n)}).join('');
 const none={unread:'Nothing unread.',vps:'No VPS activity yet.',auth:'No account activity yet.',admin:'No admin activity yet.'}[NF]||'Everything you do on your account and your VPS shows up here.';
 return head('Notifications',r.unread?r.unread+' unread':'You are all caught up','<button class="btn" data-act="notif-prefs">'+ic('sliders')+'Settings</button><button class="btn" data-act="notif-readall">Mark all read</button><button class="btn danger" data-act="notif-clear">Clear all</button>')+chips+(r.items.length?`<div class="card nlist">${list}</div>`:empty('No notifications',none))}
// ---------- shell + router
const NAV_U=[['overview','Overview','grid'],['vps','My VPS','server'],['profile','Profile','user']];
const NAV_A=[['admin','Admin Overview','grid'],['admin/users','Users','users'],['admin/vps','VPS Management','server'],['admin/nodes','Nodes','box'],['admin/monitor','System Monitoring','mon'],['admin/logs','Activity Logs','log'],['admin/settings','Settings','sliders']];
function shellHTML(adm){const nav=(adm?NAV_A:NAV_U).map(n=>`<a data-nav="${n[0]}" href="/${n[0]}">${ic(n[2])}<span>${n[1]}</span></a>`).join('');
 const extra=adm?`<a href="/overview">${ic('back')}<span>Back to User Dashboard</span></a>`:(ME.role==='admin'?`<a href="/admin">${ic('shield')}<span>Admin Panel</span></a>`:'')+`<a data-act="logout" href="/login">${ic('out')}<span>Logout</span></a>`;
 return `<div class="app"><aside class="side" id="side"><div class="brand"><span class="logo">${logoHTML()}</span><div class="bt"><b>${esc(CFG.name)}</b><span class="ver">V${esc(String(CFG.version).split('.').slice(0,2).join('.'))} • ${esc(CFG.name)}</span></div></div><nav>${nav}</nav><div class="sp2"></div><nav>${extra}</nav><div class="scredit">${creditHTML()}</div></aside>
 <div class="mainw"><header class="top"><button id="menu" class="btn ghost sm" data-act="menu">${ic('menu')}</button><div class="grow"></div><span class="live" id="live"><i></i>Live</span><button id="bell" class="btn ghost sm bell" data-act="bell" aria-label="Notifications">${ic('bell')}<i class="bcount" id="bcount" hidden>0</i></button><div class="chip"><span class="av">${esc(ME.username[0].toUpperCase())}</span><span>${esc(ME.username)}</span>${ME.role==='admin'?'<span class="bd admin">admin</span>':''}</div><button class="btn sm ghost" data-act="logout" title="Logout">${ic('out')}</button></header><main id="main"></main><footer class="foot">${creditHTML()}</footer></div></div>`}
function setActive(){const h=location.pathname.replace(/^\/+/,'');let best=null;document.querySelectorAll('[data-nav]').forEach(a=>{a.classList.remove('on');const k=a.dataset.nav;if(h===k||(k!=='admin'&&h.startsWith(k+'/'))){if(!best||k.length>best.dataset.nav.length)best=a}});if(best)best.classList.add('on')}
async function mount(adm,fn,every){clearInterval(timer);const tok=++TOK,key=adm?'a':'u';
 if(shellFor!==key||!$('#main')){app.innerHTML=shellHTML(adm);shellFor=key}startBell();setActive();$('#side').classList.remove('open');{const lv=$('#live');if(lv)lv.style.visibility=every?'visible':'hidden';const m0=$('#main');m0.className='';m0.innerHTML=SKEL}
 let last=null,first=true;
 const draw=async()=>{try{const h=await fn();if(tok!==TOK)return;tick();if(h!==last){const m=$('#main');if(!m)return;m.className=first?'anim':'';m.innerHTML=h;last=h;first=false;paint()}}catch(e){if(tok===TOK&&first){const m=$('#main');if(m)m.innerHTML=`<div class="alert">${esc(e.message)}</div>`}}};
 CUR=draw;await draw();if(every)timer=setInterval(draw,every)}
function authView(a,b){clearInterval(timer);stopBell();TOK++;shellFor=null;CUR=null;
 const fl=(n,l,t,x,icn,extra)=>`<div class="fld"><label for="${n}">${l}</label><div class="iw${t==='password'?' pw':''}">${ic(icn)}<input id="${n}" name="${n}" type="${t||'text'}" required autocomplete="${x||'off'}"${extra||''}>${t==='password'?`<button type="button" class="eye" data-act="pw-eye" aria-label="Show password" tabindex="-1">${ic('eye')}</button>`:''}</div></div>`;
 const ver='V'+String(CFG.version).split('.').slice(0,2).join('.')+' • '+CFG.name;
 const hero=`<aside class="hero"><div class="hb"><span class="logo">${logoHTML()}</span><b>${esc(CFG.name)}</b></div><div class="hm"><h2>${esc(CFG.tagline||'Sign in to your cloud')}</h2>${CFG.description?`<p>${esc(CFG.description)}</p>`:''}</div><div class="hf"><span class="ver">${esc(ver)}</span>${creditHTML()}</div></aside>`;
 const seg=on=>CFG.registration?`<div class="seg"><a href="/login" class="${on==='login'?'on':''}">Sign in</a><a href="/register" class="${on==='register'?'on':''}">Create account</a></div>`:'';
 const title=(t,s_)=>`<h1>${t}</h1>${s_?`<p class="mu asub">${s_}</p>`:'<div style="margin-bottom:22px"></div>'}`;
 const err='<div class="aerr" id="aerr" role="alert" hidden></div>',meter='<div class="pwm"><i id="pwi"></i></div><div class="pwl" id="pwl"></div>';
 const show=inner=>{app.innerHTML=`<div class="auth2">${hero}<main class="apane"><div class="awrap"><div class="acard" id="acard">${inner}</div><div class="acredit">${creditHTML()}</div></div></main></div>`;const f=$('input');if(f)f.focus()};
 if(a==='register'&&CFG.registration)show(seg('register')+title('Create your account','')+`<form data-form="register">${fl('username','Username','text','username','user')}${fl('email','Email','email','email','mail')}${fl('password','Password','password','new-password','lock',' data-strength="1"')}${meter}${fl('confirm','Confirm password','password','new-password','lock')}${err}<button class="btn pri big">Create account</button></form>`);
 else if(a==='reset'&&b)show(title('Choose a new password','')+`<form data-form="reset" data-token="${esc(b)}">${fl('password','New password','password','new-password','lock',' data-strength="1"')}${meter}${fl('confirm','Confirm new password','password','new-password','lock')}${err}<button class="btn pri big">Save password</button></form><p class="alinks"><a href="/login">Back to sign in</a></p>`);
 else if(a==='forgot')show(title('Forgot your password?','Ask an administrator to set a new password for your account.')+`<a class="btn big" href="/login">Back to sign in</a>`);
 else{if(a!=='login')history.replaceState(null,'','/login');
  const dc=CFG.discord_login?`<div class="or"><span>or</span></div><a class="btn dc big" href="/auth/discord/start?mode=login">${ic('chat')}Continue with Discord</a>`:'';
  show(seg('login')+title('Welcome back','Sign in to your account')+`<form data-form="login">${fl('identity','Username or email','text','username','user')}${fl('password','Password','password','current-password','lock')}<div class="caps" id="caps" hidden>Caps Lock is on</div>${err}<button class="btn pri big">Sign in</button></form>${dc}<p class="alinks"><a href="/forgot">Forgot password?</a></p>`)}}
function authErr(m){const e=$('#aerr'),c=$('#acard');if(e){e.textContent=m;e.hidden=false}if(c){c.classList.remove('shake');void c.offsetWidth;c.classList.add('shake')}}
function busy(f,on){const b=f.querySelector('button.pri');if(!b)return;if(on){b.dataset.l=b.innerHTML;b.disabled=true;b.innerHTML='<i class="sp"></i>'}else{b.disabled=false;b.innerHTML=b.dataset.l||b.innerHTML}}
function pwScore(p){let n=0;if(p.length>=8)n++;if(p.length>=12)n++;if(/[a-z]/.test(p)&&/[A-Z]/.test(p))n++;if(/\d/.test(p)&&/[^A-Za-z0-9]/.test(p))n++;return n}
function nav(p,rep){history[rep?'replaceState':'pushState'](null,'',p);route()}
function urlNotice(){const q=new URLSearchParams(location.search),M={disabled:'Discord login is not enabled.',denied:'Discord sign-in was cancelled.',state:'The sign-in session expired. Please try again.',failed:'Discord sign-in failed. Please try again.',nolink:'No account is linked to this Discord user, and registration is closed.',suspended:'This account is suspended.',taken:'That Discord account is already linked to another user.',busy:'Too many attempts. Try again later.'},e=q.get('error')||q.get('discord_error');
 if(e)toast(M[e]||'Discord sign-in failed.','err');if(q.get('discord')==='linked')toast('Discord account connected');if(e||q.get('discord'))history.replaceState(null,'',location.pathname)}
function route(){urlNotice();closeConsole();const p=location.pathname.replace(/^\/+|\/+$/g,'').split('/'),a=p[0],b=p[1];
 if(!ME)return authView(a,b);
 if(['login','register','forgot','reset'].includes(a)){nav('/overview',1);return}
 if(a==='vps'&&p[2]==='console')return consolePage(b);
 if(a==='admin'){if(ME.role!=='admin'){nav('/overview',1);return}
  const c=p[2],m={'':[vAdmin,10000],users:c==='new'?[vUserNew]:c&&p[3]==='edit'?[()=>vUserEdit(c)]:c?[()=>vUser(c),8000]:[vUsers],vps:c==='new'?[()=>vCreate(true)]:c?[()=>vDetail(c),4000]:[vAVps,6000],nodes:c==='new'?[vNodeNew]:[c?()=>vNode(c):vNodes,10000],monitor:[vMon,8000],logs:[vLogs],notifications:[vNotifs,10000],settings:[()=>vASet(c)]}[p[1]===undefined?'':p[1]];
  if(!m){nav('/admin',1);return}
  return mount(true,m[0],m[1])}
 switch(a){case '':case 'overview':return mount(false,vOverview,5000);
  case 'vps':if(b==='new'){nav('/vps',1);return}return b?mount(false,()=>vDetail(b),4000):mount(false,vList,4000);
  case 'profile':return mount(false,vProfile);case 'notifications':return mount(false,vNotifs,10000);case 'settings':nav('/profile',1);return;
  default:nav('/overview',1)}}
// ---------- user views
async function vOverview(){const d=await api('/overview'),u=d.user,s=d.stats,r=d.usage;
 return head('Overview','Welcome back, <b>'+esc(u.username)+'</b>')+
 `<div class="grid g3">${stat('Total VPS',s.total,'server')}${stat('Running',s.running,'pulse','var(--ok)')}${stat('Stopped',s.stopped,'box','var(--mu)')}</div>
 <div class="grid g2"><div class="card"><h3>Resource usage</h3><div class="row around">${dn(pc(r.mem_used,r.ram),'Memory in use')}${dn(Math.round(r.cpu_avg),'CPU load')}</div><div style="margin-top:12px">${kv('Allocated',r.cpu+' vCPU · '+r.ram+' MB RAM · '+r.disk+' GB disk')}${kv('Memory in use',r.mem_used+' MB')}</div></div>
 <div class="card"><h3>Account information</h3>${kv('Username',esc(u.username))}${kv('Email',esc(u.email))}${kv('Role',esc(u.role))}${kv('Member since',fd(u.created_at))}${kv('Last sign in',fd(u.last_login))}</div></div>
 <div class="row sb" style="margin:8px 0 14px"><h3 style="margin:0">My VPS</h3><a class="btn sm ghost" href="/vps">View all</a></div>
 ${d.vps.length?`<div class="grid g3">${d.vps.map(vcard).join('')}</div>`:empty('No VPS yet','Servers assigned to you appear here.')}
 <div class="card"><h3>Recent activities</h3>${d.activity.length?d.activity.map(actItem).join(''):'<p class="mu">Nothing yet.</p>'}</div>`}
async function vList(){const v=await api('/vps');return head('My VPS',v.length+' instance'+(v.length===1?'':'s'))+
 (v.length?vrows(v,false):empty('No VPS yet','Servers assigned to you appear here.'))}
async function vDetail(id){const v=await api('/vps/'+id),adm=ME.role==='admin',busy=TR.includes(v.status),run=v.status==='running',dis=x=>x?' disabled':'';
 return `<div class="vh"><div class="row wrap"><a class="back" href="${location.pathname.startsWith('/admin')?'/admin/vps':ME.id===v.user_id?'/vps':'/admin/vps'}">${ic('chev')}Back</a><div><h1>${esc(v.label||v.name)}</h1><div class="meta"><span class="bd">#${v.id}</span><span class="mu">${esc(osName(v))}</span><span class="mu">${esc(v.name)}</span>${vb(v)}</div></div></div>
 <div class="row gap wrap">
 <button class="btn"${dis(busy||run||v.suspended)} data-act="vps-op" data-op="start" data-id="${v.id}">Start</button>
 <button class="btn"${dis(busy||!run)} data-act="vps-op" data-op="stop" data-id="${v.id}">Stop</button>
 <button class="btn"${dis(busy||!run)} data-act="vps-op" data-op="restart" data-id="${v.id}">Restart</button>
 ${run&&!v.suspended?`<a class="btn" href="/vps/${v.id}/console" target="_blank" rel="noopener">${ic('term')}Console</a>`:`<button class="btn" disabled>${ic('term')}Console</button>`}
 <button class="btn"${dis(busy||v.suspended)} data-act="vps-reinstall" data-id="${v.id}" data-h="${esc(v.name)}">Reinstall</button>
 ${adm?`<button class="btn"${dis(busy)} data-act="vps-susp" data-id="${v.id}" data-op="${v.suspended?'unsuspend':'suspend'}">${v.suspended?'Unsuspend':'Suspend'}</button>`:''}
 <button class="btn danger"${dis(busy)} data-act="vps-del" data-id="${v.id}" data-h="${esc(v.name)}">Delete</button></div></div>`+(v.error?`<div class="alert">${esc(v.error)}</div>`:'')+`
 <div class="grid g3"><div class="card"><h3>Details</h3>${kv('IP address',v.ip?esc(v.ip):'—')}${kv('Operating system',esc(osName(v)))}${kv('Status',esc(v.status))}${kv('Uptime',fu(v.uptime))}${kv('Login user',esc(v.username))}${kv('Port',v.port?esc(v.port):'—')}</div>
 <div class="card"><h3>Allocation</h3>${kv('Memory',v.ram_mb+' MB')}${kv('CPU cores',v.cpu)}${kv('Disk',v.disk_gb+' GB')}${kv('Hostname',esc(v.hostname))}${kv('Created',fd(v.created_at))}</div>
 <div class="card"><h3>Live usage</h3><div class="row around">${dn(run?Math.round(v.cpu_pct||0):0,'CPU')}${dn(run?pc(v.mem_mb||0,v.ram_mb):0,'Memory')}${dn(run&&v.disk_used_mb!=null?pc(v.disk_used_mb,v.disk_gb*1024):0,'Disk')}</div></div></div>`}
async function vCreate(adm){const D=CFG.defaults;let users=[],nodes=[];
 if(adm)[users,nodes]=await Promise.all([api('/admin/users'),api('/admin/nodes')]);
 const sl=(k,l,min,max,cap,step,val)=>`<div class="fld"><label for="n_${k}">${l}</label><div class="row gap"><input type="range" data-link="${k}" min="${min}" max="${cap}" step="${step}" value="${val}" style="padding:0"><input id="n_${k}" name="${k}" type="number" min="${min}" max="${max}" value="${val}" style="width:120px" required></div></div>`;
 const os=CFG.os.map((o,i)=>`<label class="osc"><input type="radio" name="image" value="${esc(o.id)}"${i?'':' checked'}><span><b>${esc(o.name)}</b><small>${esc(o.version)}</small></span></label>`).join('');
 const owner=adm?`<div class="card" style="margin-bottom:16px"><h3><span class="n">4</span>Owner and placement</h3><div class="fld"><label for="user_id">Owner</label><select id="user_id" name="user_id">${users.filter(u=>!u.suspended).map(u=>`<option value="${u.id}"${u.id===ME.id?' selected':''}>${esc(u.username)} (${esc(u.email)})</option>`).join('')}</select></div><div class="fld"><label for="node_id">Node</label><select id="node_id" name="node_id"><option value="">Automatic (least loaded)</option>${nodes.filter(n=>n.enabled).map(n=>`<option value="${n.id}">${esc(n.name)}</option>`).join('')}</select></div></div>`:'';
 const res=adm?`<div class="card" style="margin-bottom:16px"><h3><span class="n">3</span>Resources</h3>${sl('ram_mb','RAM (MB)',128,D.max_ram,Math.min(D.max_ram,32768),128,D.ram)}${sl('cpu','CPU cores',1,D.max_cpu,Math.min(D.max_cpu,32),1,D.cpu)}${sl('disk_gb','Disk (GB)',1,D.max_disk,Math.min(D.max_disk,500),1,D.disk)}</div>`:'';
 const cn=adm?'2':'2';
 return `<a class="back" href="${adm?'/admin/vps':'/vps'}">${adm?'Back to VPS management':'Back to my VPS'}</a>`+head('Create a VPS','Name it, pick a system and deploy in a couple of minutes')+
 `<form data-form="vpsnew" data-adm="${adm?1:0}" class="cg" autocomplete="off"><div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">1</span>Name</h3><div class="fld"><label for="name">VPS name</label><input id="name" name="name" required maxlength="60"></div><div class="fld"><label for="hostname">Hostname</label><input id="hostname" name="hostname" required maxlength="42" pattern="[a-z][a-z0-9-]*" title="Lowercase letters, digits and hyphens. Must start with a letter."></div><div class="fld"><label for="port">Port</label><input id="port" name="port" type="number" required min="${D.port_min}" max="${D.port_max}"></div></div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">2</span>Operating system</h3><div class="osg">${os}</div></div>
 ${res}
 <div class="card" style="margin-bottom:16px"><h3><span class="n">${adm?5:3}</span>Login</h3><div class="fld"><label for="username">Username</label><input id="username" name="username" required pattern="[a-z_][a-z0-9_-]*" minlength="2" maxlength="31" placeholder="ubuntu"></div><div class="fld"><label for="password">Password</label><div class="row gap"><input id="password" name="password" type="password" required minlength="${D.min_pw}" maxlength="128"><button type="button" class="btn" data-act="pw-gen">Generate</button></div></div></div>
 ${owner}</div>
 <div class="card sum"><h3>Summary</h3><div class="nm" id="sm_n">your-vps</div>${kv('Hostname / container','<span id="sm_h">-</span>')}${kv('Port','<span id="sm_p">-</span>')}${kv('System','<span id="sm_os">'+esc(CFG.os[0]?CFG.os[0].label:'-')+'</span>')}${kv('CPU','<span id="sm_cpu">'+D.cpu+' vCPU</span>')}${kv('Memory','<span id="sm_ram">'+D.ram+' MB</span>')}${kv('Disk','<span id="sm_disk">'+D.disk+' GB</span>')}
 <p class="mu" style="margin:14px 0">${adm?'Setup usually takes one to three minutes while the image downloads.':'Your plan includes these resources. Setup usually takes one to three minutes.'}</p>
 <button class="btn pri" style="width:100%;justify-content:center;padding:12px">Create Instance</button><a class="btn ghost" href="${adm?'/admin/vps':'/vps'}" style="width:100%;justify-content:center;margin-top:8px">Cancel</a></div></form>`}
async function vProfile(){const u=(await api('/config')).user;ME=u;return head('Profile','Your account details and security')+`<div class="grid g2"><div class="card"><h3>Account information</h3>${kv('Username',esc(u.username))}${kv('Role',esc(u.role))}${kv('Member since',fd(u.created_at))}${kv('Last sign in',fd(u.last_login))}</div>
 <div class="card"><h3>Email address</h3><form data-form="email"><div class="fld"><label for="email">Email</label><input id="email" name="email" type="email" value="${esc(u.email)}" required></div><button class="btn pri">Save email</button></form></div><div class="card"><h3>Change password</h3><form data-form="password">${u.has_password===false?'':`<div class="fld"><label for="current">Current password</label><input id="current" name="current" type="password" required autocomplete="current-password"></div>`}<div class="fld"><label for="password">New password</label><input id="password" name="password" type="password" required minlength="${CFG.defaults.min_pw}" autocomplete="new-password"></div><div class="fld"><label for="confirm">Confirm new password</label><input id="confirm" name="confirm" type="password" required minlength="${CFG.defaults.min_pw}" autocomplete="new-password"></div><button class="btn pri">Update password</button></form></div>${CFG.discord_login||u.discord_name?`<div class="card"><h3>Discord</h3>${u.discord_name?`${kv('Connected account',esc(u.discord_name))}<button class="btn danger" style="margin-top:14px" data-act="discord-unlink">Disconnect</button>`:`<p class="mu" style="margin-bottom:14px">Connect your Discord account to sign in with one click.</p><a class="btn dc" href="/auth/discord/start?mode=link">Connect Discord</a>`}</div>`:''}</div>`}
// ---------- admin views
async function vAdmin(){const d=await api('/admin/overview'),s=d.system;
 return head('Admin overview','Platform health at a glance')+`<div class="grid g4">${stat('Users',d.users,'users')}${stat('VPS instances',d.vps,'server','var(--ac2)')}${stat('Running',d.running,'pulse','var(--ok)')}${stat('Stopped',d.stopped,'box','var(--mu)')}${stat('Nodes online',d.nodes_online+' of '+d.nodes,'mon','var(--warn)')}</div>
 <div class="grid g2"><div class="card"><h3>System resources</h3><div class="row around">${dn(Math.round(s.cpu),'CPU')}${dn(pc(s.ram_used,s.ram_total),'Memory')}${dn(pc(s.disk_used,s.disk_total),'Disk')}</div><div style="margin-top:12px">${kv('Memory',fb(s.ram_used)+' of '+fb(s.ram_total))}${kv('Disk',fb(s.disk_used)+' of '+fb(s.disk_total))}</div></div>
 <div class="card"><h3>Recent activities</h3>${d.activity.length?d.activity.map(actItem).join(''):'<p class="mu">No activity yet.</p>'}</div></div>`}
async function vUsers(){const u=await api('/admin/users');
 return head('Users',u.length+' accounts','<button class="btn pri" data-act="user-new">Create user</button>')+`<div class="card tw"><table><thead><tr><th>User</th><th>Email</th><th>Status</th><th>VPS</th><th>Last sign in</th><th></th></tr></thead><tbody>${u.map(x=>`<tr data-href="/admin/users/${x.id}"><td><a href="/admin/users/${x.id}"><b>${esc(x.username)}</b></a> ${x.role==='admin'?'<span class="bd admin">admin</span>':''}</td><td>${esc(x.email)}</td><td>${x.suspended?badge('suspended'):'<span class="bd running"><i class="dot"></i>active</span>'}</td><td>${x.vps_count}</td><td class="mu">${fd(x.last_login)}</td>
 <td><div class="row gap end"><a class="btn sm" href="/admin/users/${x.id}/edit">Edit</a><button class="btn sm" data-act="user-susp" data-id="${x.id}" data-op="${x.suspended?'unsuspend':'suspend'}">${x.suspended?'Unsuspend':'Suspend'}</button><button class="btn sm danger" data-act="user-del" data-id="${x.id}" data-n="${esc(x.username)}" data-c="${x.vps_count}">Delete</button></div></td></tr>`).join('')}</tbody></table></div>`}
const vrows=(vs,showOwner)=>`<div class="card tw"><table><thead><tr><th>VPS</th>${showOwner?'<th>Owner</th><th>Node</th>':''}<th>Status</th><th>Resources</th><th>IP</th><th></th></tr></thead><tbody>${vs.map(v=>`<tr><td><a href="${showOwner?'/admin/vps/':'/vps/'}${v.id}"><b>${esc(v.label||v.name)}</b></a><div class="mu">${esc(v.name)} · ${esc(osName(v))}</div></td>${showOwner?`<td>${esc(v.owner)}</td><td>${esc(v.node)}</td>`:''}<td>${vb(v)}</td><td class="mu">${v.cpu} vCPU · ${v.ram_mb} MB · ${v.disk_gb} GB</td><td>${esc(v.ip||'—')}</td>
 <td><div class="row gap end">${v.status==='running'?`<button class="btn sm" data-act="vps-op" data-op="stop" data-id="${v.id}">Stop</button><button class="btn sm" data-act="vps-op" data-op="restart" data-id="${v.id}">Restart</button>`:`<button class="btn sm" data-act="vps-op" data-op="start" data-id="${v.id}"${TR.includes(v.status)?' disabled':''}>Start</button>`}${ME.role==='admin'?`<button class="btn sm" data-act="vps-susp" data-id="${v.id}" data-op="${v.suspended?'unsuspend':'suspend'}">${v.suspended?'Unsuspend':'Suspend'}</button>`:''}<a class="btn sm" href="${showOwner?'/admin/vps/':'/vps/'}${v.id}">Details</a>${showOwner?`<button class="btn sm danger" data-act="vps-del" data-id="${v.id}" data-h="${esc(v.name)}">Delete</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
async function vAVps(){const v=await api('/admin/vps');return head('VPS management',v.length+' instances','<button class="btn pri" data-act="avps-new">Create Instance</button>')+(v.length?vrows(v,true):empty('No VPS yet','Create one for any user.'))}
async function vNodeNew(){const nf=(k,l)=>`<div class="fld"><label for="${k}">${l}</label><input id="${k}" name="${k}" type="number" min="0" value="0" required></div>`;
 return `<a class="back" href="/admin/nodes">Back to nodes</a>`+head('Add a node','Connect another LXD host to the panel')+
 `<form data-form="nodenew" class="cg" autocomplete="off"><div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">1</span>Node</h3><div class="fld"><label for="name">Name</label><input id="name" name="name" required maxlength="41" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" title="Letters, digits, dots, dashes and underscores"></div><div class="fld"><label for="host">Host or IP address</label><input id="host" name="host" required maxlength="102" value="127.0.0.1"></div>
 <label class="chk"><input type="checkbox" name="is_local" checked><span>This is the machine running the panel</span></label><label class="chk"><input type="checkbox" name="enabled" checked><span>Accept new VPS on this node</span></label></div>
 <div class="card" id="sshc" style="margin-bottom:16px;display:none"><h3><span class="n">2</span>SSH access</h3><p class="mu" style="margin-bottom:14px">The panel connects with its own SSH key. Run ssh-copy-id for this host from the panel server first.</p><div class="fld"><label for="ssh_user">SSH user</label><input id="ssh_user" name="ssh_user" value="root" maxlength="31"></div><div class="fld"><label for="ssh_port">SSH port</label><input id="ssh_port" name="ssh_port" type="number" min="1" max="65535" value="22"></div></div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">3</span>Storage and network</h3><div class="fld"><label for="lxc_path">Storage path (for disk statistics)</label><input id="lxc_path" name="lxc_path" value="/" required></div><div class="fld"><label for="bridge">Network bridge</label><input id="bridge" name="bridge" value="lxdbr0" required maxlength="15"></div></div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">4</span>Capacity limits</h3><p class="mu" style="margin-bottom:14px">Use 0 for unlimited.</p>${nf('max_vps','Maximum VPS')}${nf('max_ram_mb','Maximum RAM (MB)')}${nf('max_cpu','Maximum CPU cores')}${nf('max_disk_gb','Maximum disk (GB)')}</div></div>
 <div class="card sum"><h3>Summary</h3><div class="nm" id="sn_n">new-node</div>${kv('Address','<span id="sn_h">127.0.0.1</span>')}${kv('Type','<span id="sn_t">Local</span>')}
 <p class="mu" style="margin:14px 0">The node is checked right after it is added. Its status appears on the Nodes page within a few seconds.</p>
 <button class="btn pri" style="width:100%;justify-content:center;padding:12px">Add node</button><a class="btn ghost" href="/admin/nodes" style="width:100%;justify-content:center;margin-top:8px">Cancel</a></div></form>`}
async function vUserNew(){const D=CFG.defaults;
 return `<a class="back" href="/admin/users">Back to users</a>`+head('Create a user','Add an account and decide what it can do')+
 `<form data-form="usernew" class="cg" autocomplete="off"><div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">1</span>Account</h3><div class="fld"><label for="username">Username</label><input id="username" name="username" required pattern="[A-Za-z0-9_]{3,24}" minlength="3" maxlength="24" title="3 to 24 letters, digits or underscores"></div><div class="fld"><label for="email">Email</label><input id="email" name="email" type="email" required></div><div class="fld"><label for="password">Password</label><div class="row gap"><input id="password" name="password" type="password" required minlength="${D.min_pw}" maxlength="128"><button type="button" class="btn" data-act="pw-gen">Generate</button></div></div><div class="fld"><label for="confirm">Confirm password</label><input id="confirm" name="confirm" type="password" required minlength="${D.min_pw}" maxlength="128"></div></div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">2</span>Role</h3><div class="osg"><label class="osc"><input type="radio" name="role" value="user" checked><span><b>User</b><small>Manages their own VPS</small></span></label><label class="osc"><input type="radio" name="role" value="admin"><span><b>Administrator</b><small>Full platform access</small></span></label></div></div>
 </div>
 <div class="card sum"><h3>Summary</h3><div class="nm" id="su_n">new-user</div>${kv('Email','<span id="su_e">-</span>')}${kv('Role','<span id="su_r">User</span>')}
 <p class="mu" style="margin:14px 0">The user can sign in right away. Share the password securely, or create a reset link later.</p>
 <button class="btn pri" style="width:100%;justify-content:center;padding:12px">Create user</button><a class="btn ghost" href="/admin/users" style="width:100%;justify-content:center;margin-top:8px">Cancel</a></div></form>`}
async function vUser(id){const d=await api('/admin/users/'+id),u=d.user,me=u.id===ME.id,run=d.vps.filter(v=>v.status==='running').length,sum=k=>d.vps.reduce((a,v)=>a+v[k],0);
 return `<div class="vh"><div class="row wrap"><a class="back" href="/admin/users">${ic('chev')}Back</a><div><h1>${esc(u.username)}</h1><div class="meta"><span class="bd">#${u.id}</span><span class="mu">${esc(u.email)}</span>${u.role==='admin'?'<span class="bd admin">admin</span>':''}${u.suspended?badge('suspended'):'<span class="bd running"><i class="dot"></i>active</span>'}</div></div></div>
 <div class="row gap wrap"><a class="btn" href="/admin/users/${u.id}/edit">Edit</a>${me?'':`<button class="btn" data-act="user-susp" data-id="${u.id}" data-op="${u.suspended?'unsuspend':'suspend'}">${u.suspended?'Unsuspend':'Suspend'}</button>`}<button class="btn" data-act="user-signout" data-id="${u.id}" data-n="${esc(u.username)}">Sign out everywhere</button>${me?'':`<button class="btn danger" data-act="user-del" data-id="${u.id}" data-n="${esc(u.username)}" data-c="${d.vps.length}">Delete</button>`}</div></div>
 <div class="grid g3"><div class="card"><h3>Account</h3>${kv('Username',esc(u.username))}${kv('Email',esc(u.email))}${kv('Role',esc(u.role))}${kv('Joined',fd(u.created_at))}${kv('Last sign in',fd(u.last_login))}${kv('Discord',u.discord_name?esc(u.discord_name):'—')}${kv('Active sessions',d.sessions)}</div>
 <div class="card"><h3>Usage</h3>${kv('VPS instances',d.vps.length)}${kv('Running','<span class="okc">'+run+'</span>')}${kv('CPU allocated',sum('cpu')+' vCPU')}${kv('Memory allocated',sum('ram_mb')+' MB')}${kv('Disk allocated',sum('disk_gb')+' GB')}</div>
 <div class="card"><h3>Recent activity</h3>${d.activity.length?d.activity.slice(0,8).map(actItem).join(''):'<p class="mu">No activity.</p>'}</div></div>
 <h3 style="margin:8px 0 14px">VPS instances</h3>${d.vps.length?vrows(d.vps,true):'<div class="card mu">This user has no VPS.</div>'}`}
async function vUserEdit(id){const d=await api('/admin/users/'+id),u=d.user,D=CFG.defaults,role=(v,t,h)=>`<label class="osc"><input type="radio" name="role" value="${v}"${u.role===v?' checked':''}><span><b>${t}</b><small>${h}</small></span></label>`;
 return `<a class="back" href="/admin/users/${u.id}">Back to ${esc(u.username)}</a>`+head('Edit user','Change the account details of <b>'+esc(u.username)+'</b>')+
 `<form data-form="useredit" data-id="${u.id}" class="cg" autocomplete="off"><div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">1</span>Account</h3><div class="fld"><label>Username</label><input value="${esc(u.username)}" disabled></div><div class="fld"><label for="email">Email</label><input id="email" name="email" type="email" required value="${esc(u.email)}"></div></div>
 <div class="card" style="margin-bottom:16px"><h3><span class="n">2</span>Role</h3><div class="osg">${role('user','User','Manages their own VPS')}${role('admin','Administrator','Full platform access')}</div></div>
 <div class="card"><h3><span class="n">3</span>Password</h3><p class="mu" style="margin-bottom:14px">Leave both empty to keep the current password.</p><div class="fld"><label for="password">New password</label><div class="row gap"><input id="password" name="password" type="password" minlength="${D.min_pw}" maxlength="128" autocomplete="new-password"><button type="button" class="btn" data-act="pw-gen">Generate</button></div></div><div class="fld"><label for="confirm">Confirm new password</label><input id="confirm" name="confirm" type="password" minlength="${D.min_pw}" maxlength="128" autocomplete="new-password"></div></div></div>
 <div class="card sum"><h3>Summary</h3><div class="nm">${esc(u.username)}</div>${kv('Email','<span id="ue_e">'+esc(u.email)+'</span>')}${kv('Role','<span id="ue_r">'+(u.role==='admin'?'Administrator':'User')+'</span>')}${kv('Password','<span id="ue_p">Unchanged</span>')}
 <div class="merr" id="perr" hidden style="margin-top:14px"></div><button class="btn pri" style="width:100%;justify-content:center;padding:12px;margin-top:14px">Save changes</button><a class="btn ghost" href="/admin/users/${u.id}" style="width:100%;justify-content:center;margin-top:8px">Cancel</a></div></form>`}
const ncard=n=>{const s=n.stats,on=n.online;return `<div class="card vc"><div class="row sb"><div><a class="ttl" href="/admin/nodes/${n.id}">${esc(n.name)}</a><div class="mu">${esc(n.host)}${n.is_local?' · local':''}</div></div><div>${on===null?'<span class="bd"><i class="sp sm"></i>checking</span>':on?'<span class="bd running"><i class="dot"></i>online</span>':'<span class="bd error"><i class="dot"></i>offline</span>'}${n.enabled?'':'<span class="bd">disabled</span>'}</div></div>
 ${s?meter('CPU',Math.round(s.cpu),100,v=>v+'%')+meter('Memory',s.ram_used,s.ram_total,fb)+meter('Disk',s.disk_used,s.disk_total,fb):`<p class="mu">${esc(n.error||'Waiting for first reading…')}</p>`}
 <div class="specs"><span>${n.vps_count} VPS</span><span>${n.alloc.cpu} vCPU</span><span>${n.alloc.ram} MB</span><span>${n.alloc.disk} GB allocated</span></div>
 <div class="row gap wrap"><button class="btn sm" data-act="node-edit" data-id="${n.id}">Edit</button><button class="btn sm" data-act="node-toggle" data-id="${n.id}" data-on="${n.enabled?0:1}">${n.enabled?'Disable':'Enable'}</button><a class="btn sm" href="/admin/nodes/${n.id}">Details</a><button class="btn sm danger" data-act="node-del" data-id="${n.id}" data-n="${esc(n.name)}">Remove</button></div></div>`};
let NODES={};
async function vNodes(){const n=await api('/admin/nodes');NODES={};n.forEach(x=>NODES[x.id]=x);return head('Nodes',n.length+' LXC node'+(n.length===1?'':'s'),'<button class="btn pri" data-act="node-new">Add node</button>')+`<div class="grid g3">${n.map(ncard).join('')}</div>`}
async function vNode(id){const d=await api('/admin/nodes/'+id),n=d.node,s=n.stats;NODES[n.id]=n;CH['n'+id]=[{data:d.history.map(h=>h.cpu),color:'#6d5efc'},{data:d.history.map(h=>h.ram),color:'#38bdf8'},{data:d.history.map(h=>h.disk),color:'#fbbf24'}];
 return `<a class="back" href="/admin/nodes">Back to nodes</a>`+head(n.name,esc(n.host)+(n.is_local?' · local node':' · ssh '+esc(n.ssh_user)+'@'+esc(n.host)+':'+n.ssh_port),n.online?'<span class="bd running"><i class="dot"></i>online</span>':'<span class="bd error"><i class="dot"></i>offline</span>')+
 `<div class="grid g2"><div class="card"><h3>Resources</h3>${s?meter('CPU',Math.round(s.cpu),100,v=>v+'%')+meter('Memory',s.ram_used,s.ram_total,fb)+meter('Disk',s.disk_used,s.disk_total,fb)+kv('Cores',s.cores)+kv('Uptime',fu(s.uptime)):`<p class="mu">${esc(n.error||'No data')}</p>`}</div>
 <div class="card"><h3>Configuration</h3>${kv('Storage path',esc(n.lxc_path))}${kv('Bridge',esc(n.bridge))}${kv('Max VPS',n.max_vps||'unlimited')}${kv('Max RAM',n.max_ram_mb?n.max_ram_mb+' MB':'unlimited')}${kv('Max CPU',n.max_cpu||'unlimited')}${kv('Max disk',n.max_disk_gb?n.max_disk_gb+' GB':'unlimited')}</div></div>
 <div class="card" style="margin-bottom:16px"><h3>History</h3><canvas class="ln" data-line="n${id}"></canvas><div class="lg"><span><i style="background:#6d5efc"></i>CPU</span><span><i style="background:#38bdf8"></i>Memory</span><span><i style="background:#fbbf24"></i>Disk</span></div></div>
 <h3>VPS on this node</h3>${d.vps.length?vrows(d.vps,true):'<div class="card mu">No VPS assigned.</div>'}`}
async function vMon(){const n=await api('/admin/monitor');n.forEach(x=>{CH['m'+x.id]=[{data:x.history.map(h=>h.cpu),color:'#6d5efc'},{data:x.history.map(h=>h.ram),color:'#38bdf8'},{data:x.history.map(h=>h.disk),color:'#fbbf24'}]});
 return head('System monitoring','Live CPU, memory and disk per node')+n.map(x=>`<div class="card" style="margin-bottom:16px"><div class="row sb"><h3>${esc(x.name)}</h3><span>${x.online?'<span class="bd running"><i class="dot"></i>online</span>':'<span class="bd error"><i class="dot"></i>offline</span>'}</span></div>${x.stats?`<div class="row around wrap" style="margin-bottom:14px">${dn(Math.round(x.stats.cpu),'CPU')}${dn(pc(x.stats.ram_used,x.stats.ram_total),'Memory')}${dn(pc(x.stats.disk_used,x.stats.disk_total),'Disk')}</div>`:`<p class="mu">${esc(x.error||'No data yet')}</p>`}<canvas class="ln" data-line="m${x.id}"></canvas><div class="lg"><span><i style="background:#6d5efc"></i>CPU</span><span><i style="background:#38bdf8"></i>Memory</span><span><i style="background:#fbbf24"></i>Disk</span></div></div>`).join('')}
async function vLogs(){const l=await api('/admin/activity?limit=300');return head('Activity logs','Latest '+l.length+' events')+`<div class="card tw"><table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th><th>IP</th></tr></thead><tbody>${l.map(a=>`<tr><td class="mu" style="white-space:nowrap">${fd(a.created_at)}</td><td>${esc(a.username||'system')}</td><td><b>${esc(a.action)}</b></td><td class="mu">${esc(a.detail||'')}</td><td class="mu">${esc(a.ip||'')}</td></tr>`).join('')}</tbody></table></div>`}
const STABS=[['general','General','globe'],['branding','Branding','pen'],['security','Security','shield'],['system','System','cpu'],['discord','Discord','chat'],['about','About','info']];
async function vASet(tab){tab=STABS.some(t=>t[0]===tab)?tab:'general';
 const s=await api('/admin/settings'),sy=(tab==='system'||tab==='about')?await api('/admin/system'):null,se=tab==='security'?await api('/me/sessions'):[];
 const tg=(k,l)=>`<label class="chk"><input type="checkbox" name="${k}"${s[k]==='1'?' checked':''}><span>${l}</span></label>`,
  num=(k,l,mn,mx)=>`<div class="fld"><label for="${k}">${l}</label><input id="${k}" name="${k}" type="number" min="${mn}" max="${mx}" value="${esc(s[k])}" required></div>`,
  save='<button class="btn pri">Save changes</button>',
  row=(l,h,c)=>`<div class="srow"><div><b>${l}</b><small>${h}</small></div>${c}</div>`,
  sw=(k,l,h)=>`<div class="srow"><div><b>${l}</b><small>${h}</small></div><label class="sw"><input type="checkbox" name="${k}"${s[k]==='1'?' checked':''}><i></i></label></div>`;
 const panes={
 general:()=>`<div class="card scard"><div class="sh">${ic('clock')}<b>Session &amp; Access</b></div><form data-form="set">
  <div class="srow"><div><b>Public registration</b><small>Let visitors create their own account</small></div><label class="sw"><input type="checkbox" name="registration_enabled"${s.registration_enabled==='1'?' checked':''}><i></i></label></div>
  <div class="srow"><div><b>Session timeout (hours)</b><small>Auto logout after inactivity</small></div><input name="session_hours" type="number" min="1" max="720" value="${esc(s.session_hours)}" required></div>
  <div class="srow"><div><b>Max concurrent sessions</b><small>Per user limit</small></div><input name="max_sessions" type="number" min="1" max="100" value="${esc(s.max_sessions)}" required></div>
  <div class="sf">${save}</div></form></div>`,
 branding:()=>`<form data-form="set"><div class="card scard" style="margin-bottom:16px"><div class="sh">${ic('pen')}<b>Branding</b><span class="logo" id="bpv" style="margin-left:auto;background:linear-gradient(135deg,${esc(s.accent)},#38bdf8)">${s.site_icon?`<img src="${esc(s.site_icon)}" alt="">`:esc(s.logo_text)}</span></div>
  ${row('Site Name','The main title shown throughout the application',`<input name="panel_name" value="${esc(s.panel_name)}" maxlength="40" required>`)}
  ${row('Site Description','Short description shown in login page and meta tags',`<input name="site_description" value="${esc(s.site_description)}" maxlength="160" required>`)}
  ${row('Site Icon URL','Full URL to your site icon (PNG, SVG, JPG). Example: /images/icon.png',`<input name="site_icon" value="${esc(s.site_icon)}" maxlength="500" autocomplete="off">`)}
  ${row('Logo letters','Shown instead of the icon when no icon URL is set',`<input name="logo_text" value="${esc(s.logo_text)}" maxlength="3" pattern="[A-Za-z0-9]{1,3}" required>`)}
  ${row('Accent colour','Colour of buttons and highlights',`<input name="accent" type="color" value="${esc(s.accent)}" style="width:80px;height:44px;padding:4px">`)}</div>
  <div class="card scard"><div class="sh">${ic('sliders')}<b>General Configuration</b></div>
  ${row('Panel Title','The name shown in browser title',`<input name="panel_title" value="${esc(s.panel_title)}" maxlength="60" placeholder="${esc(s.panel_name)}">`)}
  ${row('Panel Description','Login page subtitle',`<input name="panel_description" value="${esc(s.panel_description)}" maxlength="120" required>`)}
  <div class="sf">${save}</div></div></form>`,
 security:()=>`<div class="card scard"><div class="sh">${ic('shield')}<b>Security Settings</b></div>
  ${row('Change Admin Password','Update administrator credentials',`<div><button type="button" class="btn" data-act="pw-change">${ic('key')}Change password</button></div>`)}
  ${row('Active Sessions','View and manage logins',`<div class="row gap wrap"><span class="okc"><i class="dot" style="background:var(--ok);box-shadow:0 0 8px var(--ok);margin-right:8px"></i>${se.length} active</span><button type="button" class="btn sm" data-act="sessions">Manage</button></div>`)}
  <form data-form="set">${sw('console_enabled','Web console','Let VPS owners open a terminal in their VPS')}
  <div class="sf">${save}</div></form></div>
  <div class="card scard" style="margin-top:16px"><div class="sh">${ic('shield')}<b>Security Overview</b></div><div class="sov">
  <div class="tile t-ok">${ic('shield')}<b>Session Auth</b><small>scrypt hashed</small></div>
  <div class="tile t-bl">${ic('key')}<b>RBAC Control</b><small>Admin/User roles</small></div>
  <div class="tile t-or">${ic('box')}<b>VM Isolation</b><small>Only own VMs</small></div>
  <div class="tile t-pu">${ic('lock')}<b>CSRF Protection</b><small>Token on every change</small></div></div></div>`,
 system:()=>`<div class="card scard"><div class="sh">${ic('cpu')}<b>System Information</b></div><div class="sysg">
  <div><div class="sec">Panel info</div>${kv('Version',esc(CFG.name)+' V'+esc(sy.version))}${kv('Framework',esc(sy.framework))}${kv('Database','SQLite3 · '+fb(sy.db_size))}${kv('Hypervisor',esc(sy.hypervisor))}${kv('Python',esc(sy.python))}${kv('Operating system',esc(sy.os))}</div>
  <div><div class="sec">Live stats</div>${kv('Total VPS',sy.vps)}${kv('Running VPS','<span class="okc">'+sy.running+'</span>')}${kv('Users',sy.users)}${kv('Nodes online',sy.nodes_online+' of '+sy.nodes)}${kv('Open consoles',sy.terminals)}${kv('Panel uptime',fu(sy.uptime))}${kv('Panel status','<span class="okc"><i class="dot" style="background:var(--ok);box-shadow:0 0 8px var(--ok);margin-right:7px"></i>Online</span>')}</div></div></div>
  <div class="card scard dz" style="margin-top:16px"><div class="sh dzh">${ic('warn')}<b>Danger Zone</b></div><div class="srow"><div><b>Stop all VPS</b><small>Emergency stop all running VPS on every node</small></div><button type="button" class="btn dzb" data-act="stop-all">${ic('bolt')}Stop All</button></div></div>`,
 discord:()=>`<form data-form="set"><div class="card scard" style="margin-bottom:16px"><div class="sh">${ic('chat')}<b>Discord login</b></div>
  ${sw('discord_auth','Sign in with Discord','Show a Discord button on the login page')}
  ${row('Client ID','From the Discord Developer Portal, on the OAuth2 page',`<input name="discord_client_id" value="${esc(s.discord_client_id)}" maxlength="25" autocomplete="off">`)}
  ${row('Client Secret',s.discord_client_secret_set?'A secret is saved. Leave empty to keep it':'Paste the client secret from the OAuth2 page',`<input name="discord_client_secret" type="password" maxlength="100" autocomplete="new-password">`)}
  ${row('Redirect URI','Add this exact URL under OAuth2, Redirects',`<div class="row gap"><input id="dcb" readonly value="${esc(s.discord_callback)}"><button type="button" class="btn" data-act="copy-val" data-t="dcb">Copy</button></div>`)}
  ${row('Redirect override','Only needed behind a proxy. Leave empty otherwise',`<input name="discord_redirect" value="${esc(s.discord_redirect)}" maxlength="300" autocomplete="off">`)}
  <div class="sf">${save}</div></div></form>`,
 about:()=>`<div class="card"><div class="row gap" style="margin-bottom:14px"><span class="logo">${logoHTML()}</span><div><h3 style="margin:0">${esc(s.panel_name)}</h3><div class="mu">Version ${esc(sy.version)}</div></div></div><p class="mu" style="margin-bottom:14px">A VPS management panel that runs real LXD containers: instances, nodes, users, monitoring and a web console in one place.</p>${kv('Version',esc(sy.version))}${kv('Built with','Python, Flask, SQLite and LXD')}${kv('Created by','Fahim')}<div style="margin-top:18px;text-align:center">${creditHTML()}</div></div>`};
 return head('Settings','Configure the platform')+`<div class="tabs">${STABS.map(t=>`<a class="tab${t[0]===tab?' on':''}" href="/admin/settings/${t[0]}">${ic(t[2])}${t[1]}</a>`).join('')}</div><div class="tabc">${panes[tab]()}</div>`}
// ---------- actions
const fld=(name,label,type,value,extra)=>Object.assign({name,label,type,value},extra||{});
A.menu=()=>$('#side').classList.toggle('open');
A.logout=async()=>{try{const r=await api('/logout','POST');CSRF=r.csrf}catch(e){}ME=null;nav('/login')};
A['avps-new']=()=>nav('/admin/vps/new');
A['pw-gen']=el=>{const i=$('#password');if(!i)return;const c='abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789',r=crypto.getRandomValues(new Uint32Array(16));i.value=Array.from(r,x=>c[x%c.length]).join('');i.type='text';{const cf=$('#confirm');if(cf){cf.value=i.value;cf.type='text'}}i.focus();i.select();toast('Password generated. Copy it before creating.')};
A['vps-op']=async el=>{const o=el.dataset.op,id=el.dataset.id;{const note={start:'The VPS will boot now.',stop:'Running processes will be interrupted.',restart:'The VPS will reboot and running processes will be interrupted.'}[o];if(!await ask(cap(o)+' VPS',[],cap(o),o==='stop',note))return}
 go(()=>api(`/vps/${id}/${o}`,'POST'),x=>x.message)};
A['vps-reinstall']=async el=>{let m='';const ok=await ask('Reinstall '+el.dataset.h,[fld('image','Operating system','select',CFG.os[0]&&CFG.os[0].id,{options:osOpts()}),fld('password','New password','password','',{min:CFG.defaults.min_pw}),fld('confirm','Type the hostname to confirm','text','',{placeholder:el.dataset.h})],'Erase and reinstall',true,'All data on this VPS will be permanently erased.',async v=>{if(v.confirm!==el.dataset.h)throw new Error('The hostname you typed does not match. Nothing was changed.');m=(await api(`/vps/${el.dataset.id}/reinstall`,'POST',{image:v.image,password:v.password})).message});if(ok){toast(m);if(CUR)CUR()}};
A['vps-del']=async el=>{let m='';const ok=await ask('Delete '+el.dataset.h,[fld('confirm','Type the hostname to confirm','text','',{placeholder:el.dataset.h})],'Delete VPS',true,'The container and all of its data will be destroyed. This cannot be undone.',async v=>{if(v.confirm!==el.dataset.h)throw new Error('The hostname you typed does not match. Nothing was deleted.');m=(await api('/vps/'+el.dataset.id,'DELETE')).message});if(ok){toast(m);nav(location.pathname.startsWith('/admin')?'/admin/vps':'/vps')}};
A['vps-pw']=async el=>{const v=await ask('Set VPS password',[fld('password','New password','password','',{min:CFG.defaults.min_pw})],'Update password',false,'Applies to your login user and root inside the VPS.');if(v)go(()=>api(`/vps/${el.dataset.id}/password`,'POST',v),x=>x.message)};
A['vps-susp']=async el=>{const o=el.dataset.op;if(!await ask(cap(o)+' VPS',[],cap(o),o==='suspend',o==='suspend'?'The VPS will be stopped and the owner cannot start it.':''))return;go(()=>api(`/vps/${el.dataset.id}/${o}`,'POST'),x=>x.message)};
A['vps-res']=async el=>{const v=await ask('Change resources',[fld('ram_mb','RAM (MB)','number',el.dataset.ram,{min:128,max:CFG.defaults.max_ram}),fld('cpu','CPU cores','number',el.dataset.cpu,{min:1,max:CFG.defaults.max_cpu}),fld('disk_gb','Disk (GB)','number',el.dataset.disk,{min:1,max:CFG.defaults.max_disk})],'Apply');if(v)go(()=>api(`/vps/${el.dataset.id}/resources`,'PUT',v),x=>x.message)};
A.console=el=>{const id=el.dataset.id;rawModal(`<div class="row sb" style="margin-bottom:12px"><h3 style="margin:0">Console</h3><button class="btn sm ghost" data-act="close">Close</button></div><div class="term"><pre id="to">Connected. Each command runs inside the VPS as root.\n</pre><form id="tf"><span class="mu">$</span><input id="ti" autocomplete="off" spellcheck="false" placeholder="type a command"></form></div>`,true);
 const to=$('#to'),ti=$('#ti'),H=[];let hi=0;ti.focus();
 ti.addEventListener('keydown',e=>{if(e.key==='ArrowUp'&&H.length){hi=Math.max(0,hi-1);ti.value=H[hi]||'';e.preventDefault()}if(e.key==='ArrowDown'){hi=Math.min(H.length,hi+1);ti.value=H[hi]||''}});
 $('#tf').addEventListener('submit',async e=>{e.preventDefault();const c=ti.value.trim();if(!c)return;H.push(c);hi=H.length;ti.value='';ti.disabled=true;to.textContent+='$ '+c+'\n';
  try{const r=await api(`/vps/${id}/exec`,'POST',{command:c});to.textContent+=r.output+(r.output&&!r.output.endsWith('\n')?'\n':'')}catch(err){to.textContent+='error: '+err.message+'\n'}
  ti.disabled=false;ti.focus();to.scrollTop=to.scrollHeight})};
A.close=closeModal;
A['user-new']=()=>nav('/admin/users/new');
A['user-susp']=async el=>{const o=el.dataset.op;if(!await ask(cap(o)+' user',[],cap(o),o==='suspend',o==='suspend'?'They are signed out and their running VPS are stopped.':''))return;go(()=>api(`/admin/users/${el.dataset.id}/${o}`,'POST'),x=>x.message)};
A['user-del']=async el=>{const d=el.dataset,f=+d.c?[fld('force','Also destroy their '+d.c+' VPS','checkbox',false)]:[];let m='';const ok=await ask('Delete '+d.n,f,'Delete user',true,'This permanently removes the account.',async v=>{m=(await api(`/admin/users/${d.id}${v.force?'?force=1':''}`,'DELETE')).message});if(ok){toast(m);if(/^\/admin\/users\/\d+/.test(location.pathname))nav('/admin/users');else if(CUR)CUR()}};
A['user-signout']=async el=>{if(!await ask('Sign out everywhere',[],'Sign out',true,el.dataset.n+' will have to sign in again on every device.'))return;go(()=>api(`/admin/users/${el.dataset.id}/sessions/revoke`,'POST'),x=>x.message)};
const nodeFields=n=>{n=n||{};return [fld('name','Name','text',n.name||''),fld('host','Host or IP','text',n.host||'127.0.0.1'),fld('is_local','This is the machine running the panel','checkbox',n.is_local),fld('ssh_user','SSH user (remote nodes)','text',n.ssh_user||'root',{required:false}),fld('ssh_port','SSH port','number',n.ssh_port||22,{min:1,max:65535}),fld('lxc_path','Storage path (disk stats)','text',n.lxc_path||'/var/lib/lxc'),fld('bridge','Network bridge','text',n.bridge||'lxcbr0'),fld('max_vps','Max VPS (0 = unlimited)','number',n.max_vps||0,{min:0}),fld('max_ram_mb','Max RAM in MB (0 = unlimited)','number',n.max_ram_mb||0,{min:0}),fld('max_cpu','Max CPU cores (0 = unlimited)','number',n.max_cpu||0,{min:0}),fld('max_disk_gb','Max disk in GB (0 = unlimited)','number',n.max_disk_gb||0,{min:0}),fld('enabled','Accept new VPS','checkbox',n.enabled==null?true:n.enabled)]};
A['node-new']=()=>nav('/admin/nodes/new');
A['node-edit']=async el=>{const n=NODES[el.dataset.id];if(!n)return;let m='';const ok=await ask('Edit '+n.name,nodeFields(n),'Save changes',false,'',async v=>{m=(await api('/admin/nodes/'+n.id,'PUT',v)).message});if(ok){toast(m);if(CUR)CUR()}};
A['node-toggle']=el=>go(()=>api('/admin/nodes/'+el.dataset.id,'PUT',{enabled:el.dataset.on==='1'}),x=>x.message);
A['node-del']=async el=>{if(!await ask('Remove '+el.dataset.n,[],'Remove node',true,'The node must not host any VPS.'))return;const r=await go(()=>api('/admin/nodes/'+el.dataset.id,'DELETE'),x=>x.message);if(r&&location.pathname.includes('/nodes/'))nav('/admin/nodes')};
// ---------- interactive console page (opens in its own tab)
let CONS=null;
const KEYS={esc:'\x1b',tab:'\t',cc:'\x03',cd:'\x04',up:'\x1b[A',down:'\x1b[B'};
const THEMES={dark:{background:'#050506',foreground:'#d7d9e0',cursor:'#8b7cff',selectionBackground:'#3b3470'},matrix:{background:'#000000',foreground:'#39ff14',cursor:'#39ff14',selectionBackground:'#1c5c10'},amber:{background:'#0b0600',foreground:'#ffb000',cursor:'#ffb000',selectionBackground:'#5c3d00'},paper:{background:'#f6f4ee',foreground:'#2b2b2b',cursor:'#2b2b2b',selectionBackground:'#cfcab8'}};
const lsGet=k=>{try{return localStorage.getItem(k)}catch(e){return null}},lsSet=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const b64u8=s=>{const b=atob(s),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u};
const loadJS=u=>new Promise((ok,no)=>{const e=document.createElement('script');e.src=u;e.nonce=NONCE;e.onload=ok;e.onerror=()=>no(new Error('Could not load '+u));document.head.appendChild(e)});
async function loadXterm(){if(window.Terminal&&window.FitAddon)return;const b=CFG.cdn;if(!b)throw new Error('The full terminal is disabled by the administrator (TERMINAL_CDN is empty).');
 const l=document.createElement('link');l.rel='stylesheet';l.href=b+'/npm/@xterm/xterm@5.5.0/css/xterm.css';document.head.appendChild(l);
 await loadJS(b+'/npm/@xterm/xterm@5.5.0/lib/xterm.js');await loadJS(b+'/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js')}
function closeConsole(){if(!CONS)return;const c=CONS;CONS=null;c.closed=true;clearTimeout(c.rt);try{c.ro&&c.ro.disconnect();c.term&&c.term.dispose()}catch(e){}if(c.sid)api('/term/'+c.sid,'DELETE').catch(()=>{});document.title=CFG.title||CFG.name||document.title}
function cSt(t,k){const e=$('#cst');if(!e)return;e.className='cst '+k;e.querySelector('em').textContent=t}
function cOv(show,msg,btn){const o=$('#cov');if(!o)return;o.classList.toggle('off',!show);if(show)o.innerHTML=(btn?'':'<i class="sp"></i>')+`<div>${esc(msg)}</div>`+(btn?'<div class="row gap wrap" style="justify-content:center"><button class="btn pri" data-act="creconnect">Reconnect</button></div>':'')}
function cTheme(c){const t=THEMES.dark;c.term.options.theme=t;const cn=$('#cn');if(cn)cn.style.setProperty('--tbg',t.background)}
async function cFlush(c){if(c.busy||!c.q||!c.sid)return;c.busy=true;const d=c.q;c.q='';try{await api(`/term/${c.sid}/write`,'POST',{d})}catch(e){}c.busy=false;if(c.q)cFlush(c)}
async function cPump(c){const sid=c.sid;while(CONS===c&&c.sid===sid&&!c.closed){
 try{const r=await api(`/term/${sid}/read?o=${c.o}`);if(c.sid!==sid)return;c.fails=0;c.o=r.o;if(r.data)c.term.write(b64u8(r.data));
  if(!r.alive){c.sid=null;cSt('Session ended','bad');cOv(true,'The session ended.',true);return}}
 catch(e){if(CONS!==c||c.sid!==sid)return;c.fails=(c.fails||0)+1;if(c.fails>4){c.sid=null;cSt('Disconnected','bad');cOv(true,'Connection lost: '+e.message,true);return}await sleep(1000)}}}
async function cConnect(c,o){c.last=o;cSt('Connecting','c');cOv(true,o.mode==='ssh'?'Signing in over SSH…':'Opening a shell…');c.o=0;if(c.sid){api('/term/'+c.sid,'DELETE').catch(()=>{});c.sid=null}c.term.reset();
 try{const r=await api(`/vps/${c.id}/term`,'POST',{cols:c.term.cols,rows:c.term.rows,mode:o.mode,host:c.ip,port:o.port,username:o.username,password:o.password});c.sid=r.sid;c.fails=0;cSt(o.mode==='ssh'?'SSH connected':'Connected','ok');cOv(false);c.term.focus();cPump(c)}
 catch(e){cSt('Disconnected','bad');cOv(true,e.message,true)}}
async function consolePage(id){clearInterval(timer);TOK++;shellFor=null;CUR=null;closeConsole();
 const fail=(m,basic)=>{app.innerHTML=`<div class="auth"><div class="card"><h3>Console unavailable</h3><p class="mu" style="margin-bottom:16px">${esc(m)}</p>${basic?`<button class="btn pri" data-act="console" data-id="${esc(id)}" style="margin-bottom:10px">Open basic console</button>`:''}<a class="btn" href="/vps/${esc(id)}" style="width:100%;justify-content:center">Back to VPS</a></div></div>`};
 let v;try{v=await api('/vps/'+id)}catch(e){return fail(e.message)}
 if(v.status!=='running')return fail('This VPS is not running. Start it first.');
 if(v.suspended)return fail('This VPS is suspended.');
 if(!CFG.console)return fail('The console is disabled by the administrator.');
 document.title=(v.label||v.name)+' - Console';
 app.innerHTML=`<div class="cn" id="cn"><header class="cbar"><a class="btn sm ghost" href="/vps/${v.id}">${ic('back')}Panel</a><b class="ctt">${ic('term')}${esc(v.label||v.name)}</b><span class="mu">${esc(v.name)}</span><span class="mu">${esc(v.ip||'')}</span><span class="cst c" id="cst"><i></i><em>Connecting</em></span><div class="grow"></div>
 <div class="row gap wrap ck"><button class="btn sm" data-act="ckey" data-key="esc">Esc</button><button class="btn sm" data-act="ckey" data-key="tab">Tab</button><button class="btn sm" data-act="ckey" data-key="cc">Ctrl+C</button><button class="btn sm" data-act="ckey" data-key="cd">Ctrl+D</button><button class="btn sm" data-act="ckey" data-key="up">Up</button><button class="btn sm" data-act="ckey" data-key="down">Down</button></div><span class="bar-sep"></span>
 <div class="row gap wrap"><button class="btn sm" data-act="cpaste">Paste</button><button class="btn sm" data-act="cclear">Clear</button><button class="btn sm" data-act="cfont" data-d="-1">A-</button><button class="btn sm" data-act="cfont" data-d="1">A+</button><button class="btn sm" data-act="cfull">Fullscreen</button><button class="btn sm" data-act="creconnect">Reconnect</button></div></header>
 <div class="cwrap"><div class="cterm" id="term"></div><div class="cov" id="cov"><i class="sp"></i><div>Loading terminal…</div></div></div></div>`;
 const c=CONS={id:v.id,ip:v.ip,user:v.username,last:null,term:null,fit:null,sid:null,o:0,q:'',busy:false,closed:false};
 try{await loadXterm()}catch(e){if(CONS===c)CONS=null;return fail(e.message+' The full terminal needs access to its script host.',true)}
 if(CONS!==c)return;
 c.term=new Terminal({cursorBlink:true,fontSize:+(lsGet('fvm_fs')||14),fontFamily:'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace',scrollback:5000});
 c.fit=new FitAddon.FitAddon();c.term.loadAddon(c.fit);c.term.open($('#term'));cTheme(c);c.fit.fit();
 c.send=d=>{c.q+=d;cFlush(c)};c.term.onData(c.send);
 c.term.onResize(z=>{clearTimeout(c.rt);c.rt=setTimeout(()=>{if(c.sid)api(`/term/${c.sid}/resize`,'POST',{cols:z.cols,rows:z.rows}).catch(()=>{})},150)});
 c.ro=new ResizeObserver(()=>{try{c.fit.fit()}catch(e){}});c.ro.observe($('#term'));
 cConnect(c,{mode:'shell'})}
A.ckey=el=>{if(CONS&&CONS.sid){CONS.send(KEYS[el.dataset.key]);CONS.term.focus()}};
A.cpaste=async()=>{try{const t=await navigator.clipboard.readText();if(t&&CONS)CONS.term.paste(t)}catch(e){toast('Clipboard access was blocked. Use Ctrl+Shift+V instead.','err')}};
A.cclear=()=>{if(CONS){CONS.term.clear();CONS.term.focus()}};
A.cfont=el=>{const c=CONS;if(!c)return;const z=Math.min(28,Math.max(10,c.term.options.fontSize+(+el.dataset.d)));c.term.options.fontSize=z;lsSet('fvm_fs',z);try{c.fit.fit()}catch(e){}};
A.cfull=()=>{if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen().catch(()=>{})};
A.creconnect=()=>{const c=CONS;if(!c)return;cConnect(c,c.last||{mode:'shell'})};
// ---------- forms + events
const fv=f=>Object.fromEntries(new FormData(f).entries());
async function authDone(r){CSRF=r.csrf;ME=r.user;nav('/overview',1)}
F.login=async f=>{busy(f,true);try{await authDone(await api('/login','POST',fv(f)))}catch(e){authErr(e.message);busy(f,false)}};
F.register=async f=>{const d=fv(f);if(d.password!==d.confirm)return authErr('Passwords do not match.');busy(f,true);try{await authDone(await api('/register','POST',d))}catch(e){authErr(e.message);busy(f,false)}};
F.reset=async f=>{const v=fv(f);if(v.password!==v.confirm)return authErr('Passwords do not match.');busy(f,true);try{await api('/reset','POST',{token:f.dataset.token,password:v.password,confirm:v.confirm});toast('Password updated. Sign in with your new password.');nav('/login')}catch(e){authErr(e.message);busy(f,false)}};
A['pw-eye']=el=>{const i=el.parentElement.querySelector('input');if(!i)return;const on=i.type==='password';i.type=on?'text':'password';el.innerHTML=ic(on?'eyeoff':'eye');el.setAttribute('aria-label',on?'Hide password':'Show password')};
document.addEventListener('input',e=>{const t=e.target,c=t.closest&&t.closest('#acard');if(!c)return;const er=$('#aerr');if(er)er.hidden=true;
 if(t.dataset&&t.dataset.strength){const n=pwScore(t.value),i=$('#pwi'),l=$('#pwl');if(i&&l){i.style.width=t.value?(Math.max(n,1)*25)+'%':'0';i.style.background=['#f87171','#f87171','#fbbf24','#34d399','#34d399'][n];l.textContent=t.value?['Too weak','Weak','Fair','Good','Strong'][n]:''}}});
document.addEventListener('keydown',e=>{const t=e.target;if(!t||t.type!=='password'||!t.closest||!t.closest('#acard'))return;const c=$('#caps');if(c)c.hidden=!(e.getModifierState&&e.getModifierState('CapsLock'))});
F.email=f=>go(async()=>{const r=await api('/me','PUT',fv(f));ME=r.user},'Email updated');
F.password=f=>{const v=fv(f);if(v.password!==v.confirm)return toast('Passwords do not match','err');go(async()=>{await api('/me/password','POST',v);f.reset()},'Password updated')};
F.set=f=>{const d={};[...f.elements].forEach(e=>{if(!e.name)return;d[e.name]=e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value});go(async()=>{await api('/admin/settings','PUT',d);Object.assign(CFG,await api('/config'));applyBrand()},'Settings saved')};
async function sessModal(){const l=await api('/me/sessions');rawModal(`<div class="row sb" style="margin-bottom:6px"><h3 style="margin:0">Active sessions</h3><button class="btn sm ghost" data-act="close">Close</button></div><p class="mu" style="margin-bottom:14px">Every browser or device that is signed in to your account.</p>
 <div class="tw"><table><thead><tr><th>Address</th><th>Signed in</th><th>Last active</th><th></th></tr></thead><tbody>${l.map(x=>`<tr><td>${esc(x.ip||'Unknown')}${x.current?' <span class="bd running"><i class="dot"></i>this device</span>':''}</td><td class="mu">${fd(x.created_at)}</td><td class="mu">${fd(x.last_seen)}</td><td>${x.current?'':`<button class="btn sm danger" data-act="sess-revoke" data-id="${esc(x.id)}">Sign out</button>`}</td></tr>`).join('')}</tbody></table></div>
 ${l.length>1?'<div class="row end" style="margin-top:14px"><button class="btn danger" data-act="sess-others">Sign out all other sessions</button></div>':''}`,true)}
A.sessions=()=>sessModal().catch(e=>toast(e.message,'err'));
A['sess-revoke']=async el=>{try{const r=await api('/me/sessions/revoke','POST',{id:el.dataset.id});toast(r.message);await sessModal();if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['sess-others']=async()=>{try{const r=await api('/me/sessions/revoke','POST',{others:true});toast(r.message);await sessModal();if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['pw-change']=async()=>{const f=[];if(ME.has_password!==false)f.push(fld('current','Current password','password',''));f.push(fld('password','New password','password','',{min:CFG.defaults.min_pw}),fld('confirm','Confirm new password','password','',{min:CFG.defaults.min_pw}));
 const ok=await ask('Change password',f,'Change password',false,'',async v=>{if(v.password!==v.confirm)throw new Error('The new passwords do not match.');await api('/me/password','POST',{current:v.current,password:v.password,confirm:v.confirm})});if(ok)toast('Password updated')};
A.bell=async()=>{const p=$('#bellp');if(p){p.remove();return}try{await bellPanel()}catch(e){toast(e.message,'err')}};
A['notif-all']=el=>{const p=$('#bellp');if(p)p.remove();nav(el.dataset.href)};
A['notif-open']=async el=>{const h=el.dataset.href;try{await api('/notifications/read','POST',{ids:[+el.dataset.id]});refreshBell()}catch(e){}const p=$('#bellp');if(p)p.remove();if(h)nav(h);else if(CUR)CUR()};
A['notif-readall']=async()=>{try{const r=await api('/notifications/read','POST',{all:true});setBell(r.unread);if($('#bellp'))await bellPanel();if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['notif-clear']=async()=>{if(!await ask('Clear all notifications',[],'Clear all',true,'This removes every notification from your list.'))return;try{await api('/notifications/clear','POST');setBell(0);if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['notif-filter']=el=>{NF=el.dataset.f;if(CUR)CUR()};
A['notif-del']=async el=>{try{const r=await api('/notifications/'+el.dataset.id,'DELETE');setBell(r.unread);if($('#bellp'))await bellPanel();if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['notif-prefs']=async()=>{try{const r=await api('/notifications/prefs'),m=new Set(r.muted),
  row=(k,t,h,on)=>`<div class="srow" style="padding:14px 0;border-bottom:1px solid #1b1b20;grid-template-columns:1fr auto"><div><b>${t}</b><small>${h}</small></div><label class="sw"><input type="checkbox" data-k="${k}"${on?' checked':''}><i></i></label></div>`;
  rawModal(`<h3>Notification settings</h3><p class="mu" style="margin-bottom:6px">Choose what you want to be told about. Failures are always shown.</p>${row('vps','VPS activity','Start, stop, create, delete and similar',!m.has('vps'))}${row('auth','Account activity','Sign-ins, password and profile changes',!m.has('auth'))}${ME.role==='admin'?row('admin','Admin actions','Users, nodes and panel settings',!m.has('admin')):''}${row('toast','Pop-up alerts','Show a pop-up when a task finishes or fails',ntoastOn())}<div class="row end gap" style="margin-top:18px"><button class="btn ghost" data-act="close">Cancel</button><button class="btn pri" data-act="notif-prefs-save">Save</button></div>`)}catch(e){toast(e.message,'err')}};
A['notif-prefs-save']=async()=>{const all=[...document.querySelectorAll('#modal input[data-k]')],muted=all.filter(i=>i.dataset.k!=='toast'&&!i.checked).map(i=>i.dataset.k),tg=all.find(i=>i.dataset.k==='toast');
 try{await api('/notifications/prefs','PUT',{muted});if(tg)lsSet('fvm_ntoast',tg.checked?'1':'0');closeModal();toast('Notification settings saved');if(CUR)CUR()}catch(e){toast(e.message,'err')}};
A['discord-unlink']=async el=>{if(!await ask('Disconnect Discord',[],'Disconnect',true,'You will sign in with your password instead.'))return;go(async()=>{await api('/me/discord/unlink','POST');ME=(await api('/config')).user},'Discord disconnected')};
A['copy-val']=async el=>{const i=$('#'+el.dataset.t);if(!i)return;try{await navigator.clipboard.writeText(i.value);toast('Copied')}catch(e){i.select();toast('Press Ctrl+C to copy','err')}};
A['stop-all']=async()=>{let m='';const ok=await ask('Stop all VPS',[fld('confirm','Type STOP to confirm','text','')],'Stop all',true,'Every running VPS on every node will be stopped. Users lose access until each one is started again.',async v=>{if(v.confirm.trim().toUpperCase()!=='STOP')throw new Error('Type STOP to confirm.');m=(await api('/admin/vps/stop-all','POST')).message});if(ok){toast(m);if(CUR)CUR()}};
document.addEventListener('input',e=>{const t=e.target,p=$('#bpv');if(!p||!t.form||t.form.dataset.form!=='set')return;const f=t.form;if(['logo_text','accent','site_icon'].includes(t.name)){const u=f.elements.site_icon.value.trim();p.innerHTML=u?`<img src="${esc(u)}" alt="">`:esc(f.elements.logo_text.value||'FVM');p.style.background=`linear-gradient(135deg,${f.elements.accent.value},#38bdf8)`}});
const FAV0=(document.querySelector('link[rel=icon]')||{}).href;
function logoHTML(){return CFG.icon?`<img src="${esc(CFG.icon)}" alt="">`:esc(CFG.logo||'FVM')}
function applyBrand(){if(/^#[0-9a-fA-F]{6}$/.test(CFG.accent||''))document.documentElement.style.setProperty('--ac',CFG.accent);document.querySelectorAll('.logo').forEach(e=>{if(e.id!=='bpv')e.innerHTML=logoHTML()});document.querySelectorAll('.bt b').forEach(e=>{e.textContent=CFG.name});document.querySelectorAll('.ver').forEach(e=>{e.textContent='V'+String(CFG.version).split('.').slice(0,2).join('.')+' • '+CFG.name});
 const fv=document.querySelector('link[rel=icon]');if(fv)fv.href=CFG.icon||FAV0;const md=document.querySelector('meta[name=description]');if(md)md.content=CFG.description||'';if(CFG.title||CFG.name)document.title=CFG.title||CFG.name}
F.nodenew=async f=>{const e=f.elements,b=f.querySelector('button.pri'),n=k=>Number(e.namedItem(k).value)||0;
 const v={name:e.namedItem('name').value,host:e.namedItem('host').value,is_local:e.namedItem('is_local').checked,enabled:e.namedItem('enabled').checked,ssh_user:e.namedItem('ssh_user').value||'root',ssh_port:n('ssh_port')||22,lxc_path:e.namedItem('lxc_path').value,bridge:e.namedItem('bridge').value,max_vps:n('max_vps'),max_ram_mb:n('max_ram_mb'),max_cpu:n('max_cpu'),max_disk_gb:n('max_disk_gb')};
 b.disabled=true;ld(1);try{const r=await api('/admin/nodes','POST',v);toast(r.message);nav('/admin/nodes/'+r.id)}catch(x){toast(x.message,'err');b.disabled=false}finally{ld(0)}};
document.addEventListener('input',e=>{const f=e.target.closest&&e.target.closest('[data-form=nodenew]');if(!f)return;const g=k=>f.elements.namedItem(k),loc=g('is_local').checked,st=(i,t)=>{const x=$('#'+i);if(x)x.textContent=t};
 $('#sshc').style.display=loc?'none':'';st('sn_n',g('name').value||'new-node');st('sn_h',g('host').value||'-');st('sn_t',loc?'Local':'Remote (SSH)')});
F.useredit=async f=>{const d=fv(f),er=$('#perr'),show=m=>{er.textContent=m;er.hidden=false};er.hidden=true;
 if(d.password&&d.password!==d.confirm)return show('The new passwords do not match.');
 const body={email:d.email,role:d.role};if(d.password){body.password=d.password;body.confirm=d.confirm}
 busy(f,true);try{const r=await api('/admin/users/'+f.dataset.id,'PUT',body);toast(r.message);nav('/admin/users/'+f.dataset.id)}catch(e){show(e.message);busy(f,false)}};
F.usernew=async f=>{const v=fv(f),b=f.querySelector('button.pri');if(v.password!==v.confirm)return toast('Passwords do not match.','err');b.disabled=true;ld(1);try{const r=await api('/admin/users','POST',v);toast(r.message);nav('/admin/users/'+r.id)}catch(e){toast(e.message,'err');b.disabled=false}finally{ld(0)}};
F.vpsnew=async f=>{const v=fv(f),adm=f.dataset.adm==='1',b=f.querySelector('button.pri');
 if(adm){v.user_id=Number(v.user_id);v.node_id=v.node_id||null;['ram_mb','cpu','disk_gb'].forEach(k=>v[k]=Number(v[k]))}
 b.disabled=true;ld(1);try{const r=await api(adm?'/admin/vps':'/vps','POST',v);toast(r.message);nav(adm?'/admin/vps':'/vps/'+r.id)}catch(e){toast(e.message,'err');b.disabled=false}finally{ld(0)}};
function sumUpdate(f){if(f.dataset.form==='useredit'){const u=fv(f),st=(i,t)=>{const e=$('#'+i);if(e)e.textContent=t};st('ue_e',u.email||'-');st('ue_r',u.role==='admin'?'Administrator':'User');st('ue_p',u.password?'Will be changed':'Unchanged');return}if(f.dataset.form==='usernew'){const u=fv(f),st=(i,t)=>{const e=$('#'+i);if(e)e.textContent=t};st('su_n',u.username||'new-user');st('su_e',u.email||'-');st('su_r',u.role==='admin'?'Administrator':'User');return}const v=fv(f),o=CFG.os.find(x=>x.id===v.image),set=(i,t)=>{const e=$('#'+i);if(e)e.textContent=t};
 set('sm_n',v.name||'your-vps');set('sm_h',v.hostname||'-');set('sm_p',v.port||'-');set('sm_os',o?o.label:'-');if(v.ram_mb!=null){set('sm_ram',v.ram_mb+' MB');set('sm_cpu',v.cpu+' vCPU');set('sm_disk',v.disk_gb+' GB')}}
document.addEventListener('input',e=>{const t=e.target,f=t.closest&&t.closest('[data-form=vpsnew],[data-form=usernew],[data-form=useredit]');if(!f)return;
 if(f.dataset.form==='vpsnew'){if(t.name==='hostname'||t.name==='username'){t.value=t.value.toLowerCase().replace(t.name==='username'?/[^a-z0-9_-]/g:/[^a-z0-9-]/g,'')}
  if(t.name==='hostname')t.dataset.t='1';if(t.name==='name'&&!f.elements.hostname.dataset.t)f.elements.hostname.value=t.value.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^[^a-z]+/,'').replace(/-+$/,'').slice(0,42)}
 if(t.dataset.link){const n=f.elements[t.dataset.link];if(n)n.value=t.value}
 else if(t.name&&f.querySelector(`[data-link="${t.name}"]`))f.querySelector(`[data-link="${t.name}"]`).value=t.value;
 sumUpdate(f)});
document.addEventListener('input',e=>{const t=e.target,f=t.form;if(!f||!f.dataset.form||!f.elements.confirm||!f.elements.password||(t.name!=='confirm'&&t.name!=='password'))return;const c=f.elements.confirm;c.setCustomValidity(c.value&&c.value!==f.elements.password.value?'Passwords do not match':'')});
document.addEventListener('submit',e=>{const f=e.target.closest('[data-form]');if(!f)return;e.preventDefault();F[f.dataset.form](f)});
document.addEventListener('click',e=>{const bp=$('#bellp');if(bp&&!e.target.closest('#bellp,#bell'))bp.remove();const tr=e.target.closest('tr[data-href]');if(tr&&!e.target.closest('a,button,input,select,label,[data-act]')){nav(tr.dataset.href);return}const l=e.target.closest('a[href^="/"]');if(l&&!l.target&&!l.dataset.act&&!l.getAttribute('href').startsWith('/auth/')&&!e.metaKey&&!e.ctrlKey&&!e.shiftKey&&e.button===0){e.preventDefault();nav(l.getAttribute('href'));return}const t=e.target.closest('[data-act]');if(!t)return;const h=A[t.dataset.act];if(h){if(t.tagName==='A'&&t.dataset.act!=='logout')e.preventDefault();if(t.dataset.act==='logout')e.preventDefault();h(t)}});
addEventListener('popstate',route);
(async function boot(){try{CFG=await api('/config');CSRF=CFG.csrf;ME=CFG.user;applyBrand()}catch(e){app.innerHTML='<div class="auth"><div class="card"><h3>Panel unavailable</h3><p class="mu">'+esc(e.message)+'</p></div></div>';return}route()})();
})();
</script></body></html>
"""


FAV_DEFAULT = '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'8\' fill=\'%236d5efc\'/%3E%3Ctext x=\'16\' y=\'22\' font-size=\'16\' font-family=\'sans-serif\' font-weight=\'700\' text-anchor=\'middle\' fill=\'white\'%3EFVM%3C/text%3E%3C/svg%3E">'


# ------------------------------------------------------------------ entrypoint
def set_password_cli(username):
    import getpass
    u = q("SELECT id FROM users WHERE username=?", (username,), one=True)
    if not u: sys.exit(f"No such user: {username}")
    p1 = getpass.getpass("New password: ")
    if len(p1) < C.MIN_PW or p1 != getpass.getpass("Repeat password: "): sys.exit(f"Passwords differ or are shorter than {C.MIN_PW} characters")
    x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(p1), u["id"])); print("Password updated.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    init_db()
    if cmd == "init": print(f"Database ready at {C.DB}"); return
    if cmd == "passwd" and len(sys.argv) > 2: return set_password_cli(sys.argv[2])
    if cmd != "run": sys.exit(__doc__)
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0:
        log.warning("Not running as root: local LXC commands usually require root (or an unprivileged LXC setup).")
    threading.Thread(target=monitor_loop, daemon=True, name="fvm-monitor").start()
    threading.Thread(target=term_reaper, daemon=True, name="fvm-term-reaper").start()
    log.info("%s %s listening on http://%s:%s  |  FVM Panel (c) 2026 Fahim", C.NAME, C.VERSION, C.HOST, C.PORT)
    if C.DEBUG: app.run(C.HOST, C.PORT, debug=True, use_reloader=False)
    else:
        from waitress import serve
        serve(app, host=C.HOST, port=C.PORT, threads=C.THREADS, ident="fvm-panel")


if __name__ == "__main__":
    main()
