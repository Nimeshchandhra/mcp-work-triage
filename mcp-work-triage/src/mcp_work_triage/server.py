"""MCP Server entrypoint registering least-privilege work triage tools with structural governance."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from mcp_work_triage.audit import AuditLogger
from mcp_work_triage.config import SecurityGovernanceError, Settings, get_read_token, get_write_token
from mcp_work_triage.github_client import GitHubReadClient, GitHubWriteClient
from mcp_work_triage.models import (
    CommitDraftRequest,
    CommitDraftResult,
    CredentialScope,
    DraftRecord,
    DraftTriageRequest,
    IssueSummary,
    IssueThread,
    Outcome,
    RiskTier,
)
from mcp_work_triage.staging_db import mark_committed, verify_and_prepare_commit, create_draft

# Create server instance
server = MCPServer("mcp-work-triage")
mcp = server  # Alias for backward compatibility with FastMCP runners


def get_audit_logger(settings: Optional[Settings] = None) -> AuditLogger:
    s = settings or Settings.from_env()
    return AuditLogger(s.audit_log_path)


def get_read_client(settings: Optional[Settings] = None) -> GitHubReadClient:
    s = settings or Settings.from_env()
    return GitHubReadClient(
        read_token=get_read_token(s),
        connect_timeout=s.request_timeout_connect,
        read_timeout=s.request_timeout_read,
        max_retries=s.max_retries,
        max_pages=s.max_pages,
        strip_markers=s.strip_private_markers,
    )


def get_write_client(settings: Optional[Settings] = None) -> GitHubWriteClient:
    s = settings or Settings.from_env()
    write_token = get_write_token(s)  # Raises SecurityGovernanceError if disabled or missing
    return GitHubWriteClient(
        write_token=write_token,
        connect_timeout=s.request_timeout_connect,
        read_timeout=s.request_timeout_read,
        max_retries=s.max_retries,
    )


# -------------------------------------------------------------------------
# Tool 1: list_issues (READ_ONLY)
# -------------------------------------------------------------------------
@server.tool()
async def list_issues(
    repo: str,
    state: str = "open",
    labels: Optional[list[str]] = None,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    """List issues from a GitHub repository using read-only credentials.
    
    Risk Tier: READ_ONLY
    Credential Scope: read_only_pat
    Structural Restriction: Read-only HTTP GET only.
    """
    settings = Settings.from_env()
    audit_logger = get_audit_logger(settings)
    run_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    call_args = {"repo": repo, "state": state, "labels": labels, "max_pages": max_pages}

    try:
        client = get_read_client(settings)
        issues = await client.list_issues(
            repo=repo, state=state, labels=labels, max_pages=max_pages
        )
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        audit_logger.log(
            tool_name="list_issues",
            risk_tier=RiskTier.READ_ONLY,
            credential_scope=CredentialScope.READ_ONLY_PAT,
            args=call_args,
            outcome=Outcome.ALLOWED,
            latency_ms=latency_ms,
            run_id=run_id,
        )
        return [i.model_dump() for i in issues]

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        audit_logger.log(
            tool_name="list_issues",
            risk_tier=RiskTier.READ_ONLY,
            credential_scope=CredentialScope.READ_ONLY_PAT,
            args=call_args,
            outcome=Outcome.ERROR,
            latency_ms=latency_ms,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise exc


# -------------------------------------------------------------------------
# Tool 2: get_issue_thread (READ_ONLY)
# -------------------------------------------------------------------------
@server.tool()
async def get_issue_thread(repo: str, issue_number: int) -> dict[str, Any]:
    """Fetch sanitized issue body and thread comments.
    
    Risk Tier: READ_ONLY
    Credential Scope: read_only_pat
    Structural Restriction: Strips internal/private markers; read-only GET.
    """
    settings = Settings.from_env()
    audit_logger = get_audit_logger(settings)
    run_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    call_args = {"repo": repo, "issue_number": issue_number}

    try:
        client = get_read_client(settings)
        thread = await client.get_issue_thread(repo=repo, issue_number=issue_number)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        audit_logger.log(
            tool_name="get_issue_thread",
            risk_tier=RiskTier.READ_ONLY,
            credential_scope=CredentialScope.READ_ONLY_PAT,
            args=call_args,
            outcome=Outcome.ALLOWED,
            latency_ms=latency_ms,
            run_id=run_id,
        )
        return thread.model_dump()

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        audit_logger.log(
            tool_name="get_issue_thread",
            risk_tier=RiskTier.READ_ONLY,
            credential_scope=CredentialScope.READ_ONLY_PAT,
            args=call_args,
            outcome=Outcome.ERROR,
            latency_ms=latency_ms,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise exc


# -------------------------------------------------------------------------
# Tool 3: draft_triage_update (HUMAN_GATED_DRAFT)
# -------------------------------------------------------------------------
@server.tool()
async def draft_triage_update(
    repo: str,
    issue_number: int,
    suggested_labels: list[str],
    triage_summary: str,
    suggested_comment: Optional[str] = None,
) -> dict[str, Any]:
    """Draft a proposed triage update into local staging DB for human review.
    
    Risk Tier: HUMAN_GATED_DRAFT
    Credential Scope: local_db_only (no GitHub write credentials)
    Structural Restriction: Forbids autonomous writes or commitments (no assignee, due date, or closing).
    """
    settings = Settings.from_env()
    audit_logger = get_audit_logger(settings)
    run_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    raw_args = {
        "repo": repo,
        "issue_number": issue_number,
        "suggested_labels": suggested_labels,
        "triage_summary": triage_summary,
        "suggested_comment": suggested_comment,
    }

    try:
        # Strict validation: ensures zero commitment keys are processed
        draft_req = DraftTriageRequest(**raw_args)

        # Staging into local SQLite database
        record = create_draft(draft_req, db_path=settings.staging_db_path)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        audit_logger.log(
            tool_name="draft_triage_update",
            risk_tier=RiskTier.HUMAN_GATED_DRAFT,
            credential_scope=CredentialScope.LOCAL_DB_ONLY,
            args=raw_args,
            outcome=Outcome.ALLOWED,
            latency_ms=latency_ms,
            run_id=run_id,
        )

        return {
            "status": "pending_human_review",
            "draft_id": record.draft_id,
            "repo": record.repo,
            "issue_number": record.issue_number,
            "suggested_labels": record.suggested_labels,
            "triage_summary": record.triage_summary,
            "suggested_comment": record.suggested_comment,
            "created_at": record.created_at,
            "message": (
                f"Triage draft {record.draft_id} staged successfully. "
                "Autonomous writing is structurally prohibited. "
                "A human operator must review and approve this draft before commitment."
            ),
        }

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        outcome = Outcome.BLOCKED if isinstance(exc, (ValueError, SecurityGovernanceError)) else Outcome.ERROR
        audit_logger.log(
            tool_name="draft_triage_update",
            risk_tier=RiskTier.HUMAN_GATED_DRAFT,
            credential_scope=CredentialScope.LOCAL_DB_ONLY,
            args=raw_args,
            outcome=outcome,
            latency_ms=latency_ms,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise exc


# -------------------------------------------------------------------------
# Tool 4: commit_approved_draft (HUMAN_GATED_WRITE)
# -------------------------------------------------------------------------
@server.tool()
async def commit_approved_draft(
    draft_id: str,
    human_approval_token: str,
) -> dict[str, Any]:
    """Execute an approved triage draft only if human approval token matches.
    
    Risk Tier: HUMAN_GATED_WRITE
    Credential Scope: scoped_write_pat (disabled by default)
    Structural Restriction: Rejects calls without human approval; never exposes commitment fields.
    """
    settings = Settings.from_env()
    audit_logger = get_audit_logger(settings)
    run_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    raw_args = {"draft_id": draft_id, "human_approval_token": human_approval_token}

    try:
        # 1. Strict schema validation
        commit_req = CommitDraftRequest(**raw_args)

        # 2. Local human-gating verification
        draft, write_idempotency_key = verify_and_prepare_commit(
            draft_id=commit_req.draft_id,
            human_approval_token=commit_req.human_approval_token,
            db_path=settings.staging_db_path,
        )

        # 3. Credential and master switch verification
        write_client = get_write_client(settings)

        # 4. Apply mutations safely (labels + comments only)
        applied_labels = await write_client.apply_labels(
            repo=draft.repo,
            issue_number=draft.issue_number,
            labels=draft.suggested_labels,
            idempotency_key=f"lbl:{write_idempotency_key}",
        )

        comment_id: Optional[int] = None
        if draft.suggested_comment:
            comment_id = await write_client.add_comment(
                repo=draft.repo,
                issue_number=draft.issue_number,
                body=draft.suggested_comment,
                idempotency_key=f"cmt:{write_idempotency_key}",
            )

        # 5. Mark as committed in local DB to prevent re-execution
        mark_committed(draft.draft_id, db_path=settings.staging_db_path)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        audit_logger.log(
            tool_name="commit_approved_draft",
            risk_tier=RiskTier.HUMAN_GATED_WRITE,
            credential_scope=CredentialScope.SCOPED_WRITE_PAT,
            args=raw_args,
            outcome=Outcome.ALLOWED,
            latency_ms=latency_ms,
            run_id=run_id,
        )

        result = CommitDraftResult(
            success=True,
            draft_id=draft.draft_id,
            repo=draft.repo,
            issue_number=draft.issue_number,
            applied_labels=applied_labels,
            comment_id=comment_id,
            committed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            idempotency_key=write_idempotency_key,
        )
        return result.model_dump()

    except SecurityGovernanceError as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        audit_logger.log(
            tool_name="commit_approved_draft",
            risk_tier=RiskTier.HUMAN_GATED_WRITE,
            credential_scope=CredentialScope.SCOPED_WRITE_PAT,
            args=raw_args,
            outcome=Outcome.BLOCKED,
            latency_ms=latency_ms,
            run_id=run_id,
            error_type="SecurityGovernanceError",
        )
        raise exc

    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        outcome = Outcome.BLOCKED if isinstance(exc, ValueError) else Outcome.ERROR
        audit_logger.log(
            tool_name="commit_approved_draft",
            risk_tier=RiskTier.HUMAN_GATED_WRITE,
            credential_scope=CredentialScope.SCOPED_WRITE_PAT,
            args=raw_args,
            outcome=outcome,
            latency_ms=latency_ms,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise exc


if __name__ == "__main__":
    server.run()
