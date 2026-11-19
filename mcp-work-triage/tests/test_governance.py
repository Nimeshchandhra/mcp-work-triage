"""Tests verifying structural write impossibility and credential scoping."""

import os
import pytest
from pydantic import ValidationError

from mcp_work_triage.config import SecurityGovernanceError, Settings, get_read_token, get_write_token
from mcp_work_triage.github_client import GitHubReadClient, GitHubWriteClient
from mcp_work_triage.models import CommitDraftRequest, DraftTriageRequest
from mcp_work_triage.server import server, draft_triage_update, commit_approved_draft
from mcp_work_triage.staging_db import approve_draft, create_draft, get_draft, init_db


def test_structural_write_impossibility_in_pydantic_schema():
    """Verify that attempting to inject commitment fields into DraftTriageRequest fails validation."""
    valid_payload = {
        "repo": "owner/repo",
        "issue_number": 42,
        "suggested_labels": ["bug", "triage"],
        "triage_summary": "Reproduced bug in login flow",
        "suggested_comment": "Thank you for the report.",
    }
    # Valid request should succeed
    req = DraftTriageRequest(**valid_payload)
    assert req.repo == "owner/repo"

    # Attempting to assign owner / assignee must raise ValidationError
    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "assignee": "alice"})

    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "assignees": ["alice", "bob"]})

    # Attempting to set milestone / due_date must raise ValidationError
    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "milestone": "v1.0"})

    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "due_date": "2026-12-01"})

    # Attempting to close or change state must raise ValidationError
    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "state": "closed"})

    with pytest.raises((ValidationError, ValueError)):
        DraftTriageRequest(**{**valid_payload, "close": True})


def test_commit_draft_request_forbids_extra_commitment_keys():
    """CommitDraftRequest must accept only draft_id and human_approval_token."""
    valid = {"draft_id": "test-uuid-123", "human_approval_token": "token-xyz"}
    req = CommitDraftRequest(**valid)
    assert req.draft_id == "test-uuid-123"

    # Any attempted write mutation parameter must fail
    with pytest.raises(ValidationError):
        CommitDraftRequest(**{**valid, "assignee": "mallory"})

    with pytest.raises(ValidationError):
        CommitDraftRequest(**{**valid, "state": "closed"})


@pytest.mark.asyncio
async def test_server_exposes_zero_commitment_tools():
    """Verify that server tool registry exposes NO autonomous write or commitment tools."""
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    # Expected tools
    assert tool_names == {
        "list_issues",
        "get_issue_thread",
        "draft_triage_update",
        "commit_approved_draft",
    }

    # Verify forbidden tools do NOT exist
    forbidden_tools = {
        "assign_owner",
        "assign_issue",
        "set_due_date",
        "close_issue",
        "merge_pr",
        "patch_issue",
        "delete_issue",
    }
    for forbidden in forbidden_tools:
        assert forbidden not in tool_names, f"Forbidden tool {forbidden} found in server!"


def test_credential_scoping_isolation():
    """Verify that read client and read token helper never expose write tokens."""
    settings = Settings(
        github_read_token="ghp_read_only",
        github_write_token="ghp_super_secret_write",
        enable_write_operations=False,
    )

    # Read token retrieval must return read token ONLY
    assert get_read_token(settings) == "ghp_read_only"

    # Write operations disabled -> raises SecurityGovernanceError
    with pytest.raises(SecurityGovernanceError, match="structurally disabled"):
        get_write_token(settings)

    # Read client has no write methods
    read_client = GitHubReadClient(read_token=settings.github_read_token)
    assert not hasattr(read_client, "apply_labels")
    assert not hasattr(read_client, "add_comment")
    assert not hasattr(read_client, "close_issue")
    assert not hasattr(read_client, "assign_owner")


@pytest.mark.asyncio
async def test_human_gated_draft_workflow(tmp_path, monkeypatch):
    """Test end-to-end human gating workflow: stage draft -> block unapproved -> approve -> commit."""
    db_path = tmp_path / "test_drafts.db"
    audit_path = tmp_path / "test_audit.jsonl"
    monkeypatch.setenv("STAGING_DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("ENABLE_WRITE_OPERATIONS", "false")

    # Step 1: Draft a triage update
    draft_result = await draft_triage_update(
        repo="octocat/Hello-World",
        issue_number=101,
        suggested_labels=["bug"],
        triage_summary="Crash on startup due to missing null check",
        suggested_comment="We have diagnosed the issue.",
    )
    draft_id = draft_result["draft_id"]
    assert draft_result["status"] == "pending_human_review"

    # Step 2: Attempt autonomous commit while in pending_human_review -> MUST FAIL
    with pytest.raises(SecurityGovernanceError, match="Autonomous write blocked"):
        await commit_approved_draft(draft_id=draft_id, human_approval_token="fake_token")

    # Step 3: Human reviews and explicitly approves draft
    record, human_token = approve_draft(draft_id, approved_by="senior_engineer", db_path=db_path)
    assert record.status == "approved"
    assert human_token is not None

    # Step 4: Attempt commit with invalid token -> MUST FAIL
    with pytest.raises(SecurityGovernanceError, match="Invalid human approval token"):
        await commit_approved_draft(draft_id=draft_id, human_approval_token="wrong_token")

    # Step 5: Attempt commit with valid token but writes disabled -> MUST FAIL
    with pytest.raises(SecurityGovernanceError, match="structurally disabled"):
        await commit_approved_draft(draft_id=draft_id, human_approval_token=human_token)
