"""Append-only, hash-chained audit log with one readable sentence per event."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from contracts.models import AuditEvent
from trust.signing import canonical_json

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._events: list[AuditEvent] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(AuditEvent(**json.loads(line)))

    @staticmethod
    def _hash(prev_hash: str, body: dict) -> str:
        return hashlib.sha256(prev_hash.encode() + canonical_json(body)).hexdigest()

    def append(self, actor: str, action: str, subject: str, summary: str, data: dict | None = None) -> AuditEvent:
        prev = self._events[-1].hash if self._events else GENESIS
        # normalise to plain JSON first, so the hash is identical after a reload from disk
        data = json.loads(canonical_json(data or {}))
        body = {
            "seq": len(self._events) + 1,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "subject": subject,
            "summary": summary,
            "data": data,
            "prev_hash": prev,
        }
        event = AuditEvent(**body, hash=self._hash(prev, body))
        self._events.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return event

    def events(self, subject: str | None = None) -> list[AuditEvent]:
        if not subject:
            return list(self._events)
        return [e for e in self._events if e.subject == subject or subject in json.dumps(e.data)]

    def verify(self) -> dict:
        prev = GENESIS
        for e in self._events:
            body = e.model_dump(mode="json", exclude={"hash"})
            # timestamps were hashed in their original isoformat string
            body["ts"] = e.ts.isoformat(timespec="seconds")
            if e.prev_hash != prev or self._hash(prev, body) != e.hash:
                return {"valid": False, "broken_at": e.seq, "count": len(self._events)}
            prev = e.hash
        return {"valid": True, "broken_at": None, "count": len(self._events)}

    def reset(self) -> None:
        self._events = []
        if self.path.exists():
            self.path.unlink()
