"""Signed offers - the trust protocol.

Every offer gets a SHA-256 hash over its canonical JSON and an Ed25519 signature over that hash.
An external agent can fetch the public key from /.well-known/aimo-agent-gateway.json and verify
that price and availability claims were issued by the gateway and not changed afterwards.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def content_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


class Signer:
    def __init__(self, key_path: Path):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            self._key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        else:
            self._key = Ed25519PrivateKey.generate()
            key_path.write_bytes(
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        self._public = self._key.public_key()

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._key.sign(data)).decode()

    def verify(self, data: bytes, signature_b64: str) -> bool:
        try:
            self._public.verify(base64.b64decode(signature_b64), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    def public_key_b64(self) -> str:
        raw = self._public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()
