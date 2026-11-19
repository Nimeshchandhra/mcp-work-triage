"""Local SQLite staging storage for human-gated triage drafts."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid

from mcp_work_triage.config import SecurityGovernanceError
from mcp_work_triage.models import DraftRecord, DraftTriageRequest


def init_db(db_path: Path | str = "drafts.db") -> None:
    """Initialize the SQLite schema for staging drafts."""
    path = Path(db_path)
    if path.parent:
        path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(path)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                suggested_labels TEXT NOT NULL,
                triage_summary TEXT NOT NULL,
                suggested_comment TEXT,
                status TEXT NOT NULL DEFAULT 'pending_human_review',
                created_at TEXT NOT NULL,
                approval_token TEXT,
                approved_by TEXT,
                idempotency_key TEXT NOT NULL UNIQUE
            );
            """
        )
        conn.commit()


def create_draft(req: DraftTriageRequest, db_path: Path | str = "drafts.db") -> DraftRecord:
    """Stage a new triage draft locally with status='pending_human_review'.
    
    Physically detached from GitHub write credentials.
    """
    init_db(db_path)
    draft_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    # Deterministic staging idempotency key based on content
    staging_key = hashlib.sha256(
        f"{req.repo}#{req.issue_number}#{req.triage_summary}".encode("utf-8")
    ).hexdigest()

    labels_json = json.dumps(req.suggested_labels)

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        # Handle existing duplicate draft deterministically
        cursor.execute("SELECT draft_id FROM drafts WHERE idempotency_key = ?", (staging_key,))
        existing = cursor.fetchone()
        if existing:
            # If identical draft already exists, fetch and return it
            existing_id = existing[0]
            existing_record = get_draft(existing_id, db_path)
            if existing_record:
                return existing_record

        cursor.execute(
            """
            INSERT INTO drafts (
                draft_id, repo, issue_number, suggested_labels,
                triage_summary, suggested_comment, status,
                created_at, approval_token, approved_by, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending_human_review', ?, NULL, NULL, ?)
            """,
            (
                draft_id,
                req.repo,
                req.issue_number,
                labels_json,
                req.triage_summary,
                req.suggested_comment,
                created_at,
                staging_key,
            ),
        )
        conn.commit()

    return DraftRecord(
        draft_id=draft_id,
        repo=req.repo,
        issue_number=req.issue_number,
        suggested_labels=req.suggested_labels,
        triage_summary=req.triage_summary,
        suggested_comment=req.suggested_comment,
        status="pending_human_review",
        created_at=created_at,
        approval_token=None,
        approved_by=None,
        idempotency_key=staging_key,
    )


def get_draft(draft_id: str, db_path: Path | str = "drafts.db") -> Optional[DraftRecord]:
    """Retrieve a staged draft record by ID."""
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))
        row = cursor.fetchone()
        if not row:
            return None

        return DraftRecord(
            draft_id=row["draft_id"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            suggested_labels=json.loads(row["suggested_labels"]),
            triage_summary=row["triage_summary"],
            suggested_comment=row["suggested_comment"],
            status=row["status"],
            created_at=row["created_at"],
            approval_token=row["approval_token"],
            approved_by=row["approved_by"],
            idempotency_key=row["idempotency_key"],
        )


def approve_draft(
    draft_id: str,
    approved_by: str,
    db_path: Path | str = "drafts.db",
) -> tuple[DraftRecord, str]:
    """Human gate action: explicitly approves a draft and generates a secret approval token.
    
    Returns the updated record and the generated human_approval_token.
    """
    init_db(db_path)
    token = secrets.token_urlsafe(32)

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM drafts WHERE draft_id = ?", (draft_id,))
        row = cursor.fetchone()
        if not row:
            raise KeyError(f"Draft with ID '{draft_id}' not found.")

        cursor.execute(
            """
            UPDATE drafts
            SET status = 'approved',
                approved_by = ?,
                approval_token = ?
            WHERE draft_id = ?
            """,
            (approved_by, token, draft_id),
        )
        conn.commit()

    record = get_draft(draft_id, db_path)
    if not record:
        raise RuntimeError("Failed to retrieve draft after approval.")
    return record, token


def verify_and_prepare_commit(
    draft_id: str,
    human_approval_token: str,
    db_path: Path | str = "drafts.db",
) -> tuple[DraftRecord, str]:
    """Verify human approval and generate deterministic write idempotency key.
    
    Enforces that NO draft can be committed without explicit human approval.
    """
    draft = get_draft(draft_id, db_path)
    if not draft:
        raise SecurityGovernanceError(f"Draft with ID '{draft_id}' does not exist.")

    if draft.status == "pending_human_review":
        raise SecurityGovernanceError(
            f"Autonomous write blocked: Draft '{draft_id}' is currently 'pending_human_review'. "
            "A human must explicitly review and approve it before any mutation can occur."
        )

    if draft.status == "committed":
        raise SecurityGovernanceError(
            f"Draft '{draft_id}' has already been committed. Re-execution blocked."
        )

    if draft.status != "approved":
        raise SecurityGovernanceError(
            f"Draft '{draft_id}' has invalid status '{draft.status}' for commitment."
        )

    if not draft.approval_token or draft.approval_token != human_approval_token:
        raise SecurityGovernanceError(
            "Invalid human approval token. Commitment rejected."
        )

    # Deterministic write idempotency key (sha256 of draft_id + target_issue)
    write_idempotency_key = hashlib.sha256(
        f"{draft.draft_id}+{draft.repo}#{draft.issue_number}".encode("utf-8")
    ).hexdigest()

    return draft, write_idempotency_key


def mark_committed(draft_id: str, db_path: Path | str = "drafts.db") -> DraftRecord:
    """Mark a draft as committed to prevent re-execution."""
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE drafts SET status = 'committed' WHERE draft_id = ?",
            (draft_id,),
        )
        conn.commit()

    record = get_draft(draft_id, db_path)
    if not record:
        raise RuntimeError(f"Draft '{draft_id}' not found after marking committed.")
    return record


def list_drafts(
    repo: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Path | str = "drafts.db",
) -> list[DraftRecord]:
    """List staged drafts with optional filtering."""
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = "SELECT * FROM drafts WHERE 1=1"
        params: list[Any] = []
        if repo:
            query += " AND repo = ?"
            params.append(repo)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [
            DraftRecord(
                draft_id=r["draft_id"],
                repo=r["repo"],
                issue_number=r["issue_number"],
                suggested_labels=json.loads(r["suggested_labels"]),
                triage_summary=r["triage_summary"],
                suggested_comment=r["suggested_comment"],
                status=r["status"],
                created_at=r["created_at"],
                approval_token=r["approval_token"],
                approved_by=r["approved_by"],
                idempotency_key=r["idempotency_key"],
            )
            for r in rows
        ]
