"""Web Push: completion notifications that reach a phone even when the page
is closed or the screen is locked (the page's own SSE-driven alert can't -
mobile browsers stop running backgrounded tabs).

The VAPID key pair is generated on first use and kept in the data dir, so
existing subscriptions stay valid across rebuilds."""
from __future__ import annotations

import base64
import json
import os
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("h3machine.push")

# Push services (FCM, Apple, Mozilla) require a contact in the VAPID claims;
# the site URL keeps any personal address out of it.
# Contact the push services may use: the site's public URL (or a mailto:), ROCSY_PUBLIC_URL in BunnyBytes/.env.
VAPID_SUBJECT = os.getenv("ROCSY_PUBLIC_URL") or "mailto:rocsy@users.noreply.github.com"


class PushService:
    def __init__(self, data_dir: Path, store: Any):
        self.key_path = data_dir / "vapid_private.pem"
        self.store = store
        self._vapid: Any = None

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
                vapid_claims={"sub": VAPID_SUBJECT}, ttl=6 * 3600, headers={"Urgency": "high"}, timeout=15,
            )
            return True
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                self.store.delete_push_subscription(subscription.get("endpoint", ""))
                log.info("Removed expired push subscription (%s)", status)
            else:
                log.warning("Push delivery failed (%s): %s", status, str(exc)[:200])
        except Exception:  # noqa: BLE001 - a notification must never break job handling
            log.exception("Push delivery failed")
        return False

    def broadcast(self, payload: dict[str, Any]) -> int:
        return sum(self.send(subscription, payload) for subscription in self.store.push_subscriptions())
