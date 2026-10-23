"""Settings -> Security -> Connections: who has reached Rocsy, anonymised.

A visitor is an IP address, but the IP itself is never stored or shown: it is
HMAC'd with the cookie secret into an id, and the id picks a nickname
("Amber Otter"). The country comes from Cloudflare's CF-IPCountry header
(local visits have none). Signed-in browsers are tracked by a random session
id carried in the session cookie, so one can be revoked without changing the
shared password; a revoked visitor is also refused at /login until restored.

Scanners: a request for a path Rocsy never serves (/.env, /wp-login.php, ...)
flags that visitor as a bot and, with the Security setting on, blocks it. Only
remote visitors without a signed-in browser can be auto-blocked, and restoring
one marks it trusted so it isn't blocked again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from threading import Lock

from honeypot import DECOY_PATH

ADJECTIVES = (
    "Amber", "Azure", "Brave", "Bright", "Calm", "Cheery", "Clever", "Cobalt", "Cosmic", "Crimson",
    "Curious", "Dapper", "Dusky", "Eager", "Fuzzy", "Gentle", "Gilded", "Glad", "Golden", "Hazel",
    "Humble", "Indigo", "Jade", "Jolly", "Keen", "Lively", "Lucky", "Lunar", "Mellow", "Merry",
    "Misty", "Mossy", "Nimble", "Noble", "Olive", "Peachy", "Plucky", "Polar", "Quiet", "Quick",
    "Rosy", "Rusty", "Sandy", "Scarlet", "Silver", "Sleepy", "Snowy", "Solar", "Spry", "Starry",
    "Stormy", "Sunny", "Swift", "Teal", "Tidy", "Velvet", "Violet", "Wild", "Windy", "Wise",
    "Woolly", "Zany", "Zesty", "Breezy",
)
ANIMALS = (
    "Otter", "Badger", "Falcon", "Fox", "Heron", "Ibis", "Jaguar", "Koala", "Lemur", "Lynx",
    "Marten", "Moose", "Newt", "Ocelot", "Owl", "Panda", "Puffin", "Quokka", "Raven", "Robin",
    "Seal", "Shrew", "Sloth", "Stoat", "Swan", "Tapir", "Tiger", "Toucan", "Turtle", "Vole",
    "Walrus", "Wombat", "Wren", "Yak", "Zebra", "Alpaca", "Beaver", "Bison", "Bunny", "Camel",
    "Crane", "Dingo", "Dolphin", "Eagle", "Ferret", "Finch", "Gecko", "Gibbon", "Hare", "Hedgehog",
    "Kestrel", "Kiwi", "Lark", "Llama", "Magpie", "Mink", "Narwhal", "Orca", "Osprey", "Pika",
    "Possum", "Roc", "Salamander", "Squirrel",
)

MAX_VISITORS = 500
# Auto-blocked scanners are forgotten after a while and capped (oldest first): a bot
# rotating addresses would otherwise grow connections.json without limit.
AUTO_BLOCK_TTL = 30 * 86400
MAX_AUTO_BLOCKED = 2000
# A new visitor marks the file dirty; rewrite it at most this often (admin actions write at once).
MIN_FLUSH_GAP = 5.0
VISITOR_TTL = 90 * 86400
FLUSH_INTERVAL = 30.0
_COUNTRY = re.compile(r"^[A-Z]{2}$")
# Paths no Rocsy page or API uses -- vulnerability scanners and exploit kits.
PROBE = re.compile(
    r"^/\.(env|git|svn|hg|aws|ssh|docker|ds_store|vscode|idea|htaccess|htpasswd)"
    r"|\.(php\d?|asp|aspx|jsp|cgi|env|bak|old|sql|ini|cfg|yml|yaml|swp)(/|$)"
    r"|^/(wp-|wordpress|xmlrpc|phpmyadmin|pma/|myadmin|cgi-bin|boaform|hnap1|actuator|server-status"
    r"|owa/|ecp/|autodiscover|vendor/|solr/|manager/html|geoserver|remote/login|\+cscoe\+|global-protect"
    r"|druid|telescope|_profiler|containers/json|v2/_catalog|setup\.cgi|goform|shell|console/|webui|cdn-cgi/bm)",
    re.IGNORECASE)
PROBES_KEPT = 4


def device_label(user_agent: str) -> str:
    ua = user_agent or ""
    browser = next((name for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                             ("Chrome/", "Chrome"), ("Safari/", "Safari")) if token in ua), "")
    system = next((name for token, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                            ("Windows", "Windows"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"),
                                            ("Linux", "Linux")) if token in ua), "")
    if not browser and not system:
        return "Script or bot" if ua else "Unknown device"
    return " · ".join(part for part in (browser, system) if part)


class ConnectionTracker:
    def __init__(self, path: Path, secret: str, session_ttl: int) -> None:
        self.path = path
        self._key = hashlib.sha256(b"rocsy-connections|" + secret.encode()).digest()
        self.session_ttl = session_ttl
        self._lock = Lock()
        self._dirty = False
        self._flushed = 0.0
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        self.visitors: dict[str, dict] = data.get("visitors", {}) if isinstance(data, dict) else {}
        self.sessions: dict[str, dict] = data.get("sessions", {}) if isinstance(data, dict) else {}

    # --- identity -------------------------------------------------------------
    def visitor_id(self, ip: str) -> str:
        return hmac.new(self._key, ip.encode(), hashlib.sha256).hexdigest()[:16]

    def _nickname(self, vid: str) -> str:
        taken = {v.get("nick") for v in self.visitors.values()}
        n = int(vid, 16)
        base = f"{ADJECTIVES[n % len(ADJECTIVES)]} {ANIMALS[(n // len(ADJECTIVES)) % len(ANIMALS)]}"
        nick, i = base, 2
        while nick in taken:
            nick, i = f"{base} {i}", i + 1
        return nick

    # --- recording ------------------------------------------------------------
    def record(self, ip: str, country: str, local: bool, user_agent: str, sid: str | None, revision: str | None,
               count: bool = True, path: str = "", block_scanners: bool = False, fetch_site: str = "") -> str:
        now = time.time()
        vid = self.visitor_id(ip)
        country = country.upper() if _COUNTRY.match(country.upper() or "") else ""
        with self._lock:
            visitor = self.visitors.get(vid)
            if visitor is None:
                visitor = self.visitors[vid] = {"nick": self._nickname(vid), "first": now, "hits": 0, "blocked": False}
                self._dirty = True
            visitor.update(last=now, local=local, hits=visitor.get("hits", 0) + count, agent=device_label(user_agent))
            if country:
                visitor["country"] = country
            if path and not local and not sid and PROBE.search(path):
                probes = [p for p in visitor.get("probes", []) if p != path[:80]]
                visitor["probes"] = (probes + [path[:80]])[-PROBES_KEPT:]
                # Another site can make your browser request a probe path (<img src=".../wp-login.php">)
                # without your cookie; browsers mark that cross-site, scanners send no Sec-Fetch-Site at all.
                # The decoy .env is left unblocked so a scanner can go on to use it (honeypot.py bans it then).
                if (block_scanners and fetch_site not in ("cross-site", "same-site") and not DECOY_PATH.search(path)
                        and not visitor.get("blocked") and not visitor.get("trusted")
                        and not any(s.get("visitor") == vid and not s.get("revoked") for s in self.sessions.values())):
                    visitor.update(blocked=True, auto=True, blocked_at=now)
                self._dirty = True
            if sid:
                session = self.sessions.get(sid)
                if session is None:
                    session = self.sessions[sid] = {"created": now, "revoked": False}
                    self._dirty = True
                if session.get("visitor") != vid:
                    self._dirty = True
                session.update(visitor=vid, last=now, device=device_label(user_agent), revision=revision or "legacy")
            self._maybe_flush(now)
        return vid

    def is_revoked(self, sid: str) -> bool:
        session = self.sessions.get(sid)
        return bool(session and session.get("revoked"))

    def is_blocked(self, ip: str) -> bool:
        visitor = self.visitors.get(self.visitor_id(ip))
        return bool(visitor and visitor.get("blocked"))

    # --- admin actions ----------------------------------------------------------
    def revoke(self, vid: str) -> int:
        with self._lock:
            visitor = self.visitors.get(vid)
            if visitor is None:
                raise KeyError(vid)
            visitor.update(blocked=True, auto=False)
            count = 0
            for session in self.sessions.values():
                if session.get("visitor") == vid and not session.get("revoked"):
                    session["revoked"] = True
                    count += 1
            self._flush()
            return count

    def ban(self, ip: str, reason: str) -> None:
        """Permanent block (never expires like scanner auto-blocks): used the honeypot's decoy credentials."""
        vid = self.visitor_id(ip)
        with self._lock:
            visitor = self.visitors.setdefault(vid, {"nick": self._nickname(vid), "first": time.time(), "hits": 0})
            visitor.update(blocked=True, auto=False, trusted=False, blocked_at=time.time(),
                           probes=(visitor.get("probes", []) + [reason[:80]])[-PROBES_KEPT:])
            for session in self.sessions.values():
                if session.get("visitor") == vid:
                    session["revoked"] = True
            self._flush()

    def end(self, sid: str) -> None:
        """Signed out. The session is kept as revoked (hidden from the list) until the
        cookie would have expired anyway: dropping it made a copy of the cookie valid
        again, since an unknown session id isn't revoked."""
        with self._lock:
            session = self.sessions.setdefault(sid, {"created": time.time()})
            session.update(revoked=True, ended=True)
            self._flush()

    def restore(self, vid: str) -> None:
        with self._lock:
            visitor = self.visitors.get(vid)
            if visitor is None:
                raise KeyError(vid)
            # Restored by hand: never auto-block this visitor again.
            visitor.update(blocked=False, auto=False, trusted=True)
            self._flush()

    def clear(self, current_vid: str, revision: str, password_set: bool) -> int:
        """Forget everyone who is neither revoked nor signed in (has no password)."""
        now = time.time()
        with self._lock:
            signed_in = {s.get("visitor") for s in self.sessions.values()
                         if not s.get("revoked") and s.get("revision") == revision
                         and now - s.get("created", 0) < self.session_ttl}
            drop = [vid for vid, v in self.visitors.items()
                    if not v.get("blocked") and vid != current_vid and vid not in signed_in and password_set]
            for vid in drop:
                del self.visitors[vid]
            for sid in [sid for sid, s in self.sessions.items() if s.get("visitor") in drop]:
                del self.sessions[sid]
            self._flush()
            return len(drop)

    # --- listing ----------------------------------------------------------------
    def summary(self, current_vid: str, current_sid: str | None, revision: str, password_set: bool) -> dict:
        now = time.time()
        with self._lock:
            self._prune(now)
            people = []
            for vid, visitor in self.visitors.items():
                devices = []
                for sid, session in self.sessions.items():
                    if session.get("visitor") != vid or session.get("ended"):
                        continue
                    active = (not session.get("revoked") and session.get("revision") == revision
                              and now - session.get("created", 0) < self.session_ttl)
                    devices.append({"device": session.get("device", ""), "last": session.get("last", 0),
                                    "active": active, "current": sid == current_sid})
                devices.sort(key=lambda d: d["last"], reverse=True)
                active = sum(d["active"] for d in devices)
                probes = visitor.get("probes", [])
                bot = not active and not visitor.get("trusted") and (
                    bool(probes) or visitor.get("agent") in ("Script or bot", "Unknown device"))
                people.append({
                    "id": vid, "nickname": visitor.get("nick", "Visitor"), "country": visitor.get("country", ""),
                    "local": visitor.get("local", False), "first": visitor.get("first", 0), "last": visitor.get("last", 0),
                    "hits": visitor.get("hits", 0), "blocked": visitor.get("blocked", False),
                    "has_password": active > 0 or (not password_set and not visitor.get("blocked")),
                    "active_devices": active, "devices": devices[:6], "you": vid == current_vid,
                    "bot": bot, "probes": probes, "auto_blocked": bool(visitor.get("blocked") and visitor.get("auto")),
                })
        # Signed-in people first, then busiest.
        people.sort(key=lambda p: (not p["has_password"], -p["hits"], -p["last"]))
        return {"people": people, "total": len(people),
                "with_password": sum(p["has_password"] for p in people),
                "revoked": sum(p["blocked"] for p in people), "bots": sum(p["bot"] for p in people),
                "auto_blocked": sum(p["auto_blocked"] for p in people), "password_set": password_set}

    # --- persistence ------------------------------------------------------------
    def _prune(self, now: float) -> None:
        for sid in [s for s, v in self.sessions.items() if now - v.get("created", 0) > self.session_ttl]:
            del self.sessions[sid]
        in_use = {v.get("visitor") for v in self.sessions.values()}
        stale = [vid for vid, v in self.visitors.items()
                 if vid not in in_use and not v.get("blocked") and now - v.get("last", 0) > VISITOR_TTL]
        for vid in stale:
            del self.visitors[vid]
        auto = sorted((v.get("blocked_at", v.get("last", 0)), vid) for vid, v in self.visitors.items()
                      if v.get("blocked") and v.get("auto"))
        expired = [vid for at, vid in auto if now - at > AUTO_BLOCK_TTL]
        for vid in expired + [vid for _, vid in auto if vid not in expired][:max(0, len(auto) - len(expired) - MAX_AUTO_BLOCKED)]:
            del self.visitors[vid]
        if len(self.visitors) > MAX_VISITORS:
            drop = sorted((v.get("last", 0), vid) for vid, v in self.visitors.items()
                          if vid not in in_use and not v.get("blocked"))
            for _, vid in drop[:len(self.visitors) - MAX_VISITORS]:
                del self.visitors[vid]

    def _maybe_flush(self, now: float) -> None:
        if now - self._flushed > (MIN_FLUSH_GAP if self._dirty else FLUSH_INTERVAL):
            self._flush()

    def _flush(self) -> None:
        self._prune(time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".connections-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"visitors": self.visitors, "sessions": self.sessions}, stream)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._dirty = False
        self._flushed = time.time()

    def flush(self) -> None:
        with self._lock:
            self._flush()


def new_session_id() -> str:
    return secrets.token_urlsafe(12)
