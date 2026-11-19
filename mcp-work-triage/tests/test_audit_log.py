"""Tests verifying audit logging: deterministic entries, structural fields, and reconstructability."""

import json
import pytest

from mcp_work_triage.audit import AuditLogger, canonical_json_hash
from mcp_work_triage.config import SecurityGovernanceError
from mcp_work_triage.models import CredentialScope, Outcome, RiskTier
from mcp_work_triage.server import commit_approved_draft, draft_triage_update


def test_canonical_json_hash_determinism():
    """Verify that argument hashing produces identical SHA256 hashes regardless of key ordering."""
    dict_a = {"repo": "owner/repo", "issue_number": 1, "labels": ["bug"]}
    dict_b = {"labels": ["bug"], "repo": "owner/repo", "issue_number": 1}

    hash_a = canonical_json_hash(dict_a)
    hash_b = canonical_json_hash(dict_b)

    assert hash_a == hash_b
    assert len(hash_a) == 64


def test_audit_logger_writes_and_reads_entries(tmp_path):
    """Verify that AuditLogger appends structured JSON lines and reads them back."""
    log_file = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_file)

    entry = logger.log(
        tool_name="test_tool",
        risk_tier=RiskTier.READ_ONLY,
        credential_scope=CredentialScope.READ_ONLY_PAT,
        args={"repo": "owner/repo"},
        outcome=Outcome.ALLOWED,
        latency_ms=12.5,
    )

    assert log_file.exists()
    entries = logger.read_entries()
    assert len(entries) == 1
    assert entries[0].tool_name == "test_tool"
    assert entries[0].risk_tier == RiskTier.READ_ONLY
    assert entries[0].credential_scope == CredentialScope.READ_ONLY_PAT
    assert entries[0].outcome == Outcome.ALLOWED
    assert entries[0].latency_ms == 12.5
    assert entries[0].run_id == entry.run_id


@pytest.mark.asyncio
async def test_tool_invocations_generate_audit_trail(tmp_path, monkeypatch):
    """Verify that actual server tools record deterministic audit log entries for allowed and blocked events."""
    db_path = tmp_path / "audit_test_drafts.db"
    log_path = tmp_path / "audit_test.jsonl"
    monkeypatch.setenv("STAGING_DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log_path))
    monkeypatch.setenv("ENABLE_WRITE_OPERATIONS", "false")

    # 1. Successful draft triage update -> should emit ALLOWED entry
    await draft_triage_update(
        repo="owner/repo",
        issue_number=10,
        suggested_labels=["enhancement"],
        triage_summary="Proposed refactor of logging pipeline",
    )

    # 2. Blocked commit attempt (draft not approved) -> should emit BLOCKED entry
    with pytest.raises(SecurityGovernanceError):
        await commit_approved_draft(
            draft_id="non-existent-id",
            human_approval_token="any_token",
        )

    # Read and inspect log entries
    logger = AuditLogger(log_path)
    entries = logger.read_entries()
    assert len(entries) == 2

    # Entry 1: draft_triage_update
    assert entries[0].tool_name == "draft_triage_update"
    assert entries[0].risk_tier == RiskTier.HUMAN_GATED_DRAFT
    assert entries[0].credential_scope == CredentialScope.LOCAL_DB_ONLY
    assert entries[0].outcome == Outcome.ALLOWED
    assert entries[0].latency_ms >= 0

    # Entry 2: blocked commit_approved_draft
    assert entries[1].tool_name == "commit_approved_draft"
    assert entries[1].risk_tier == RiskTier.HUMAN_GATED_WRITE
    assert entries[1].credential_scope == CredentialScope.SCOPED_WRITE_PAT
    assert entries[1].outcome == Outcome.BLOCKED
    assert entries[1].error_type == "SecurityGovernanceError"

    # Verify no raw secrets or passwords in the log file
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
        assert "ghp_" not in content
        assert "password" not in content.lower()
