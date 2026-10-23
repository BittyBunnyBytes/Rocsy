"""Decoy .env: scanners that fetch /.env get a believable file whose credentials are
random per install and match nothing real. Anyone who then signs in with the decoy
password, calls /Llama with the decoy key or sends a cookie signed with the decoy
COOKIE_SECRET has proven they are a scanner and is banned for good.

The decoy values live in data/honeypot.json (0600), never in the real .env."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer

# What scanners ask for. Only these get the decoy; every other dotfile path is a 404.
DECOY_PATH = re.compile(
    r"^/(?:(?:app|api|backend|server|src|public|web|www|admin|laravel|config|docker|core|site|old|new|prod)/)?"
    r"\.env(?:\.(?:local|prod|production|dev|development|staging|backup|bak|save|old|example))?$",
    re.IGNORECASE)

_WORDS = ("Maple", "Harbor", "Cobalt", "Juniper", "Falcon", "Copper", "Willow", "Summit", "Ember", "Lotus",
          "Orchid", "Granite", "Velvet", "Meadow", "Raven", "Saffron", "Tundra", "Indigo", "Cedar", "Comet")


def _human_password() -> str:
    # Looks like something a person picked, so a bot won't skip it as a placeholder.
    return f"{secrets.choice(_WORDS)}{secrets.choice(_WORDS).lower()}{secrets.randbelow(90) + 10}!"


class Honeypot:
    def __init__(self, path: Path, is_real_password) -> None:
        self.path = path
        try:
            values = json.loads(path.read_text())
            if not all(isinstance(values.get(k), str) and values[k]
                       for k in ("password", "cookie_secret", "llama_api_key", "queue_secret")):
                raise ValueError("incomplete")
        except (FileNotFoundError, ValueError):
            values = self._create(is_real_password)
        self.values = values
        self._serializer = URLSafeTimedSerializer(values["cookie_secret"], salt="bb_session")

    def _create(self, is_real_password) -> dict:
        password = _human_password()
        while is_real_password(password):  # astronomically unlikely, but never ban the owner
            password = _human_password()
        values = {"password": password, "cookie_secret": secrets.token_urlsafe(48),
                  "llama_api_key": "sk-" + secrets.token_hex(24), "queue_secret": secrets.token_hex(32)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".honeypot-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(values, stream)
                stream.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return values

    def env_file(self) -> str:
        v = self.values
        return (
            "# Rocsy\n"
            f"MASTER_PASSWORD={v['password']}\n"
            f"COOKIE_SECRET={v['cookie_secret']}\n"
            "COOKIE_TTL_DAYS=30\n"
            f"LLAMA_API_KEY={v['llama_api_key']}\n"
            f"QUEUE_INTERNAL_SECRET={v['queue_secret']}\n"
            "GATEWAY_PORT=8080\n"
        )

    def is_decoy_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), self.values["password"].encode("utf-8"))

    def is_decoy_key(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), self.values["llama_api_key"].encode("utf-8"))

    def is_decoy_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            self._serializer.loads(value)
            return True
        except BadSignature:
            return False
