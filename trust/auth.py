"""Sign-in for the two portals: buyers (one account per customer) and Valio Aimo staff.

A session is a small cookie signed with HMAC-SHA256, so it survives restarts and needs no session table.
The signing secret is created once in data/runtime/. Passwords come from .env (see gateway/config.py);
with no passwords configured (local development), signing in needs no password.

Roles decide what a request may do; gateway/app.py checks them on every endpoint:
    buyer -> order as its own customer, see its own offers and orders
    valio -> review queue, customers and limits, audit trail, demo reset
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

Role = Literal["buyer", "valio"]
COOKIE = "aimo_session"
SESSION_SECONDS = 12 * 3600


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    role: Role
    name: str  # buyer: the customer's name; valio: the staff member's name
    customer_id: Optional[str] = None  # buyers only

    @property
    def actor(self) -> str:
        """How this principal appears in the audit trail."""
        return f"customer:{self.customer_id}" if self.role == "buyer" else f"human:{self.name}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    def __init__(self, secret_path: Path):
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if not secret_path.exists():
            secret_path.write_bytes(secrets.token_bytes(32))
        self._secret = secret_path.read_bytes()

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())

    def issue(self, who: Principal, now: float | None = None) -> str:
        body = {"r": who.role, "n": who.name, "c": who.customer_id, "exp": int((now or time.time()) + SESSION_SECONDS)}
        payload = _b64(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
        return f"{payload}.{self._mac(payload)}"

    def read(self, token: str | None, now: float | None = None) -> Principal | None:
        """The principal in a valid, unexpired token; None for anything else."""
        if not token or token.count(".") != 1:
            return None
        payload, mac = token.split(".")
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        try:
            body = json.loads(_unb64(payload))
        except (ValueError, UnicodeDecodeError):
            return None
        if body.get("exp", 0) < (now or time.time()) or body.get("r") not in ("buyer", "valio"):
            return None
        if body["r"] == "buyer" and not body.get("c"):
            return None
        return Principal(role=body["r"], name=str(body.get("n") or ""), customer_id=body.get("c"))


def password_ok(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode(), expected.encode())
