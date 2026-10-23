"""Site-wide notification hub: one Web Push subscription per device (the
gateway's root-scope service worker, /sw.js) for every service, with
per-service, per-event switches on the Settings page's Notifications tab.

Event sources:
- Vid (H3 Machine) and Trainer send their own events to POST /internal/notify
  (signed with QUEUE_INTERNAL_SECRET) - they know their job semantics best,
  e.g. Agentic staging approval.
- Krea, Mus and Queue are watched here (`watch_services`): their job lists
  are polled on loopback and state changes become events.

The VAPID key pair is generated on first use and kept in the data dir, so
subscriptions survive rebuilds."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("gateway.notify")

# Contact the push services may use: the site's public URL (or a mailto:), ROCSY_PUBLIC_URL in BunnyBytes/.env.
VAPID_SUBJECT = os.getenv("ROCSY_PUBLIC_URL") or "mailto:rocsy@users.noreply.github.com"

# service -> (label, icon, {event: (label, default_on)})
CATALOG: dict[str, dict[str, Any]] = {
    "Vid": {"label": "Video", "note": "Video generation (H3 Machine)", "events": {
        "complete": ("Video ready", True), "failed": ("Generation failed", True),
        "candidates": ("Candidates ready to pick", True),
        "agentic_previews": ("Agentic previews ready", True), "agentic_approval": ("Agentic previews need approval", True),
        "agentic_final": ("Agentic session finished", True), "agentic_failed": ("Agentic session failed", True)}},
    "Krea": {"label": "Image", "note": "Image generation", "events": {
        "complete": ("Image ready", True), "failed": ("Generation failed", True)}},
    "Mus": {"label": "Music", "note": "Music generation", "events": {
        "complete": ("Track ready", True), "failed": ("Generation failed", True)}},
    "Trainer": {"label": "Trainer", "note": "LoRA training", "events": {
        "complete": ("Training finished", True), "failed": ("Training failed", True)}},
    "Agent": {"label": "Master Agent", "note": "Agentic sessions", "events": {
        "done": ("Session finished", True), "failed": ("Session failed", True),
        "loop": ("Loop detected (warning only)", True)}},
    "Models": {"label": "Models", "note": "Model downloads and updates", "events": {
        "downloaded": ("Download finished", True), "updates": ("Updates available", True)}},
    "Queue": {"label": "Queue", "note": "Shared GPU queue", "events": {
        "started": ("A queued job got the GPU", False)}},
}

DEFAULT_ICON = "/static/icons/rocsy-v2-192.png"
DEFAULT_BADGE = "/static/icons/rocsy-v2-badge-96.png"


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class NotificationHub:
    def __init__(self, data_dir: Path):
        self.key_path = data_dir / "vapid_private.pem"
        self.path = data_dir / "notifications.json"
        self._vapid: Any = None
        # Pushes run in worker threads and drop dead subscriptions while the
        # API adds new ones: every read-modify-write of the file holds this.
        self._lock = threading.RLock()

    # ---- storage
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("prefs", {})
        data.setdefault("subscriptions", [])
        return data

    def prefs(self) -> dict[str, dict[str, bool]]:
        saved = self._load()["prefs"]
        return {service: {event: bool(saved.get(service, {}).get(event, default))
                          for event, (_, default) in spec["events"].items()}
                for service, spec in CATALOG.items()}

    def save_prefs(self, changes: dict[str, Any]) -> dict[str, dict[str, bool]]:
        with self._lock:
            data = self._load()
            for service, events in (changes or {}).items():
                if service not in CATALOG or not isinstance(events, dict):
                    continue
                for event, on in events.items():
                    if event in CATALOG[service]["events"]:
                        data["prefs"].setdefault(service, {})[event] = bool(on)
            _atomic_write(self.path, data)
        return self.prefs()

    def devices(self) -> list[dict[str, Any]]:
        return [{"endpoint": sub["endpoint"], "label": sub.get("label", "Device"), "created": sub.get("created")}
                for sub in self._load()["subscriptions"]]

    def subscribe(self, subscription: dict[str, Any], label: str) -> None:
        if not isinstance(subscription, dict) or not str(subscription.get("endpoint", "")).startswith("https://"):
            raise ValueError("invalid subscription")
        keys = subscription.get("keys") or {}
        if not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("invalid subscription keys")
        with self._lock:
            data = self._load()
            data["subscriptions"] = [sub for sub in data["subscriptions"] if sub["endpoint"] != subscription["endpoint"]]
            data["subscriptions"].append({"endpoint": subscription["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                                          "label": label[:80] or "Device", "created": time.time()})
            _atomic_write(self.path, data)

    def unsubscribe(self, endpoint: str) -> None:
        with self._lock:
            data = self._load()
            data["subscriptions"] = [sub for sub in data["subscriptions"] if sub["endpoint"] != endpoint]
            _atomic_write(self.path, data)

    # ---- push
    def _key(self) -> Any:
        with self._lock:
            return self._load_key()

    def _load_key(self) -> Any:
        if self._vapid is None:
            from py_vapid import Vapid

            if self.key_path.is_file():
                self._vapid = Vapid.from_file(str(self.key_path))
            else:
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self._vapid = Vapid()
                self._vapid.generate_keys()
                self._vapid.save_key(str(self.key_path))
                self.key_path.chmod(0o600)
        return self._vapid

    def public_key(self) -> str:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw = self._key().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> bool:
        from pywebpush import WebPushException, webpush

        try:
            webpush(subscription_info={"endpoint": subscription["endpoint"], "keys": subscription["keys"]},
                    data=json.dumps(payload), vapid_private_key=self._key(), vapid_claims={"sub": VAPID_SUBJECT},
                    ttl=6 * 3600, headers={"Urgency": "high"}, timeout=15)
            return True
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                self.unsubscribe(subscription["endpoint"])
                log.info("Removed expired push subscription (%s)", status)
            else:
                log.warning("Push delivery failed (%s): %s", status, str(exc)[:200])
        except Exception:  # noqa: BLE001 - a notification must never break anything else
            log.exception("Push delivery failed")
        return False

    def broadcast(self, payload: dict[str, Any], endpoint: str | None = None) -> int:
        subscriptions = [sub for sub in self._load()["subscriptions"] if endpoint in (None, sub["endpoint"])]
        return sum(self._send(sub, payload) for sub in subscriptions)

    async def notify(self, service: str, event: str, title: str, body: str = "", url: str = "",
                     tag: str = "", icon: str = "", badge: str = "") -> int:
        """Delivers one service event if its switch is on. Returns devices reached."""
        if service not in CATALOG or event not in CATALOG[service]["events"]:
            raise ValueError(f"unknown event {service}/{event}")
        if not self.prefs()[service][event]:
            return 0
        payload = {"title": title[:120], "body": body[:300], "tag": tag[:120] or f"bb-{service}-{event}-{time.time():.0f}",
                   "url": url if url.startswith("/") else f"/{service}/", "icon": icon or DEFAULT_ICON,
                   "badge": badge or DEFAULT_BADGE, "service": service}
        return await asyncio.to_thread(self.broadcast, payload)


# ---- watchers for services that don't send events themselves

_TERMINAL = {"complete", "failed", "cancelled"}


async def _poll_jobs(client: httpx.AsyncClient, url: str, key: str = "jobs") -> list[dict[str, Any]] | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
        data = json.loads(response.content, strict=False)  # prompts can hold raw control chars
    except Exception:  # noqa: BLE001 - a service being down is normal
        return None
    jobs = data if isinstance(data, list) else data.get(key) if isinstance(data, dict) else None
    # Only dict entries: one malformed item would otherwise raise in the watcher and end it for good.
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else None


def _summary(text: Any) -> str:
    return " ".join(str(text or "").split())[:140]


async def watch_services(hub: NotificationHub, upstreams: dict[str, str], interval: float = 5.0) -> None:
    """Polls Krea / Mus job lists and the GPU queue; notifies on transitions
    it actually saw (a job first seen already finished never notifies)."""
    states: dict[str, dict[str, str]] = {"Krea": {}, "Mus": {}, "Queue": {}}
    primed: set[str] = set()
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            for service, labels in (("Krea", ("Image ready", "Image generation failed")),
                                    ("Mus", ("Track ready", "Music generation failed"))):
                jobs = await _poll_jobs(client, f"{upstreams[service]}/{service}/api/jobs")
                if jobs is None:
                    continue
                seen = states[service]
                for job in jobs:
                    job_id, state = str(job.get("id")), str(job.get("state") or "")
                    before = seen.get(job_id)
                    seen[job_id] = state
                    if service not in primed or before is None or before == state or before in _TERMINAL:
                        continue
                    if state in ("complete", "failed"):
                        detail = job.get("error") if state == "failed" else (job.get("prompt") or job.get("style"))
                        try:
                            await hub.notify(service, state, labels[state == "failed"], _summary(detail),
                                             url=f"/{service}/", tag=f"bb-{service}-{job_id}")
                        except Exception:  # noqa: BLE001
                            log.exception("%s notification failed", service)
                live = {str(job.get("id")) for job in jobs}
                for job_id in [key for key in seen if key not in live]:
                    del seen[job_id]
                primed.add(service)

            entries = await _poll_jobs(client, f"{upstreams['Queue']}/Queue/api/queue", key="entries")
            if entries is not None:
                seen = states["Queue"]
                for entry in entries:
                    entry_id, state = str(entry.get("entry_id")), str(entry.get("state") or "")
                    if "Queue" in primed and seen.get(entry_id) == "waiting" and state == "active":
                        app = str(entry.get("app") or "")
                        try:
                            await hub.notify("Queue", "started", "Your queued job got the GPU",
                                             f"{app}: {_summary(entry.get('label'))}".strip(": "),
                                             url=f"/{app}/" if app in CATALOG else "/Queue/", tag=f"bb-queue-{entry_id}")
                        except Exception:  # noqa: BLE001
                            log.exception("Queue notification failed")
                    seen[entry_id] = state
                live = {str(entry.get("entry_id")) for entry in entries}
                for entry_id in [key for key in seen if key not in live]:
                    del seen[entry_id]
                primed.add("Queue")
            await asyncio.sleep(interval)
