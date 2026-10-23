"""Web Push: "training finished" notifications that reach a phone even when the
Trainer page is closed or the screen is locked (ported from H3 Machine's push.py).

The VAPID key pair is generated on first use and kept in the data dir, so
existing subscriptions stay valid across rebuilds. Subscriptions live in
/data/push_subscriptions.json (endpoint -> subscription)."""
from __future__ import annotations

import base64
import json
import os
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("trainer.push")

# Push services (FCM, Apple, Mozilla) require a contact in the VAPID claims;
# the site URL keeps any personal address out of it.
# Contact the push services may use: the site's public URL (or a mailto:), ROCSY_PUBLIC_URL in BunnyBytes/.env.
VAPID_SUBJECT = os.getenv("ROCSY_PUBLIC_URL") or "mailto:rocsy@users.noreply.github.com"


class PushService:
    def __init__(self, data_dir: Path):
        self.key_path = data_dir / "vapid_private.pem"
        self.subs_path = data_dir / "push_subscriptions.json"
        self._lock = threading.Lock()
        self._vapid: Any = None

    # ---- subscriptions ----
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.subs_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, subs: dict[str, dict[str, Any]]) -> None:
        tmp = self.subs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(subs, indent=1))
        tmp.replace(self.subs_path)

    def subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load().values())

    def save_subscription(self, subscription: dict[str, Any]) -> None:
        with self._lock:
            subs = self._load()
            subs[subscription["endpoint"]] = subscription
            self._save(subs)

    def delete_subscription(self, endpoint: str) -> None:
        with self._lock:
            subs = self._load()
            if subs.pop(endpoint, None) is not None:
                self._save(subs)

    # ---- sending ----
    def _key(self) -> Any:
        if self._vapid is None:
            from py_vapid import Vapid

            if self.key_path.is_file():
                self._vapid = Vapid.from_file(str(self.key_path))
            else:
                self._vapid = Vapid()
                self._vapid.generate_keys()
                self._vapid.save_key(str(self.key_path))
                self.key_path.chmod(0o600)
        return self._vapid

    def public_key(self) -> str:
        """The applicationServerKey the browser subscribes with (base64url, uncompressed P-256 point)."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw = self._key().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> bool:
        """Deliver one notification; drops the subscription when the push
        service says it's gone. Returns whether it was accepted."""
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription_info=subscription, data=json.dumps(payload), vapid_private_key=self._key(),
                vapid_claims={"sub": VAPID_SUBJECT}, ttl=12 * 3600, headers={"Urgency": "high"}, timeout=15,
            )
            return True
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                self.delete_subscription(subscription.get("endpoint", ""))
                log.info("Removed expired push subscription (%s)", status)
            else:
                log.warning("Push delivery failed (%s): %s", status, str(exc)[:200])
        except Exception:  # noqa: BLE001 - a notification must never break job handling
            log.exception("Push delivery failed")
        return False

    def broadcast(self, payload: dict[str, Any]) -> int:
        return sum(self.send(subscription, payload) for subscription in self.subscriptions())
