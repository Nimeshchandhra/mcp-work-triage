"""Append-only structured JSONL audit logger for deterministic governance tracking."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp_work_triage.models import AuditLogEntry, CredentialScope, Outcome, RiskTier


def canonical_json_hash(args: Any) -> str:
    """Generate deterministic SHA256 of canonical JSON-serialized arguments."""
    try:
        # Sort keys and remove whitespace for determinism
        serialized = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        serialized = str(args)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AuditLogger:
    """Thread-safe append-only audit logger emitting structured JSONL entries."""

    def __init__(self, log_path: Path | str = "audit_log.jsonl"):
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        # Ensure parent directory exists
        if self.log_path.parent:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        tool_name: str,
        risk_tier: RiskTier,
        credential_scope: CredentialScope,
        args: Any,
        outcome: Outcome,
        latency_ms: float,
        run_id: Optional[str] = None,
        error_type: Optional[str] = None,
        timestamp_utc: Optional[str] = None,
    ) -> AuditLogEntry:
        """Record an immutable audit event."""
        run_id = run_id or str(uuid.uuid4())
        ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
        args_hash = canonical_json_hash(args)

        entry = AuditLogEntry(
            run_id=run_id,
            timestamp_utc=ts,
            tool_name=tool_name,
            risk_tier=risk_tier,
            credential_scope=credential_scope,
            args_sha256=args_hash,
            outcome=outcome,
            latency_ms=round(latency_ms, 2),
            error_type=error_type,
        )

        line = entry.model_dump_json() + "\n"

        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()

        return entry

    def read_entries(self) -> list[AuditLogEntry]:
        """Read and parse all audit log entries."""
        if not self.log_path.exists():
            return []
        entries: list[AuditLogEntry] = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(AuditLogEntry.model_validate_json(line))
        return entries
