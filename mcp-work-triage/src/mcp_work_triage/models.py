"""Strict Pydantic schemas for mcp-work-triage enforcing structural governance."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskTier(str, Enum):
    """Risk tiers for MCP tools enforcing least-privilege classification."""
    READ_ONLY = "READ_ONLY"
    HUMAN_GATED_DRAFT = "HUMAN_GATED_DRAFT"
    HUMAN_GATED_WRITE = "HUMAN_GATED_WRITE"


class CredentialScope(str, Enum):
    """Credential scopes for operations."""
    READ_ONLY_PAT = "read_only_pat"
    LOCAL_DB_ONLY = "local_db_only"
    SCOPED_WRITE_PAT = "scoped_write_pat"


class Outcome(str, Enum):
    """Outcome status for tool invocations."""
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    ERROR = "error"


# Forbidden keys that must NEVER be passed or mutated autonomously
FORBIDDEN_COMMITMENT_KEYS = frozenset({
    "assignee",
    "assignees",
    "milestone",
    "due_on",
    "due_date",
    "state",
    "status",
    "close",
    "closed",
    "owner",
    "priority_override",
})


class IssueSummary(BaseModel):
    """Summary of a GitHub issue for read-only listings."""
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    state: str = "open"
    labels: list[str] = Field(default_factory=list)
    author: str
    created_at: str
    comments_count: int = 0
    url: str


class IssueComment(BaseModel):
    """Single sanitized comment on an issue thread."""
    model_config = ConfigDict(extra="forbid")

    id: int
    author: str
    body: str
    created_at: str


class IssueThread(BaseModel):
    """Full thread representation of an issue with sanitized body and comments."""
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    body: str
    author: str
    state: str = "open"
    labels: list[str] = Field(default_factory=list)
    created_at: str
    comments: list[IssueComment] = Field(default_factory=list)
    url: str


class DraftTriageRequest(BaseModel):
    """Request to stage a proposed triage update locally.
    
    FORBIDS any autonomous commitment writes (assignee, milestone, due_date, closing).
    """
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(..., description="Repository in format 'owner/repo'")
    issue_number: int = Field(..., description="GitHub Issue number")
    suggested_labels: list[str] = Field(
        default_factory=list, description="Labels proposed for triage"
    )
    triage_summary: str = Field(..., description="Objective diagnostic summary of the issue")
    suggested_comment: Optional[str] = Field(
        default=None, description="Proposed response comment awaiting human approval"
    )

    @model_validator(mode="before")
    @classmethod
    def reject_commitment_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found_forbidden = [k for k in data.keys() if k.lower() in FORBIDDEN_COMMITMENT_KEYS]
            if found_forbidden:
                raise ValueError(
                    f"Structural governance violation: Commitment fields {found_forbidden} are "
                    "strictly forbidden. Autonomous agents cannot assign owners, set due dates, "
                    "or close issues."
                )
        return data


class DraftRecord(BaseModel):
    """Local staging record for a proposed triage update."""
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    repo: str
    issue_number: int
    suggested_labels: list[str]
    triage_summary: str
    suggested_comment: Optional[str] = None
    status: str = "pending_human_review"
    created_at: str
    approval_token: Optional[str] = None
    approved_by: Optional[str] = None
    idempotency_key: str


class CommitDraftRequest(BaseModel):
    """Request to commit an already-approved draft to GitHub.
    
    Requires a valid human approval token. Cannot take direct mutation parameters.
    """
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(..., description="UUID of the staged draft")
    human_approval_token: str = Field(
        ..., description="Secret approval token generated during human review"
    )

    @model_validator(mode="before")
    @classmethod
    def reject_extra_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found_forbidden = [k for k in data.keys() if k.lower() in FORBIDDEN_COMMITMENT_KEYS]
            if found_forbidden:
                raise ValueError(
                    f"Structural governance violation: Unauthorized commitment fields {found_forbidden} "
                    "detected in commit request."
                )
        return data


class CommitDraftResult(BaseModel):
    """Result of committing an approved draft to GitHub."""
    model_config = ConfigDict(extra="forbid")

    success: bool
    draft_id: str
    repo: str
    issue_number: int
    applied_labels: list[str]
    comment_id: Optional[int] = None
    committed_at: str
    idempotency_key: str


class AuditLogEntry(BaseModel):
    """Structured deterministic log entry for every tool invocation."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    timestamp_utc: str
    tool_name: str
    risk_tier: RiskTier
    credential_scope: CredentialScope
    args_sha256: str
    outcome: Outcome
    latency_ms: float
    error_type: Optional[str] = None
