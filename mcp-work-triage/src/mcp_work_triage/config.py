"""Configuration management enforcing strict credential scoping and tier isolation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class SecurityGovernanceError(Exception):
    """Raised when an action violates structural governance or credential scoping."""
    pass


class Settings(BaseModel):
    """Application settings loaded from environment with strict credential separation."""

    # READ credentials (public_repo or issues:read scope only)
    github_read_token: Optional[str] = Field(
        default=None,
        description="Read-only GitHub Personal Access Token"
    )

    # WRITE credentials (strictly isolated; disabled by default)
    github_write_token: Optional[str] = Field(
        default=None,
        description="Scoped GitHub write token for applying labels/comments only"
    )

    # Master switch to prevent any network write paths unless explicitly opted in
    enable_write_operations: bool = Field(
        default=False,
        description="Master switch: if False, all write operations are structurally blocked"
    )

    # Storage paths
    staging_db_path: Path = Field(
        default_factory=lambda: Path(os.environ.get("STAGING_DB_PATH", "drafts.db"))
    )
    audit_log_path: Path = Field(
        default_factory=lambda: Path(os.environ.get("AUDIT_LOG_PATH", "audit_log.jsonl"))
    )

    # Resilient HTTP Client settings
    request_timeout_connect: float = 5.0
    request_timeout_read: float = 15.0
    max_retries: int = 3
    max_pages: int = 10
    strip_private_markers: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables."""
        read_token = os.environ.get("GITHUB_READ_TOKEN")
        write_token = os.environ.get("GITHUB_WRITE_TOKEN")
        enable_writes = os.environ.get("ENABLE_WRITE_OPERATIONS", "false").lower() in (
            "true", "1", "yes"
        )
        staging_db = Path(os.environ.get("STAGING_DB_PATH", "drafts.db"))
        audit_log = Path(os.environ.get("AUDIT_LOG_PATH", "audit_log.jsonl"))
        strip_markers = os.environ.get("STRIP_PRIVATE_MARKERS", "true").lower() in (
            "true", "1", "yes"
        )

        return cls(
            github_read_token=read_token,
            github_write_token=write_token,
            enable_write_operations=enable_writes,
            staging_db_path=staging_db,
            audit_log_path=audit_log,
            request_timeout_connect=float(os.environ.get("REQUEST_TIMEOUT_CONNECT", "5.0")),
            request_timeout_read=float(os.environ.get("REQUEST_TIMEOUT_READ", "15.0")),
            max_retries=int(os.environ.get("MAX_RETRIES", "3")),
            max_pages=int(os.environ.get("MAX_PAGES", "10")),
            strip_private_markers=strip_markers,
        )


def get_read_token(settings: Optional[Settings] = None) -> Optional[str]:
    """Retrieve ONLY the read-scoped token. Read paths NEVER receive write credentials."""
    s = settings or Settings.from_env()
    return s.github_read_token


def get_write_token(settings: Optional[Settings] = None) -> str:
    """Retrieve the write token ONLY if write operations are explicitly enabled and token is present.
    
    Raises SecurityGovernanceError if write operations are disabled or token is missing.
    """
    s = settings or Settings.from_env()
    if not s.enable_write_operations:
        raise SecurityGovernanceError(
            "Write operations are structurally disabled by configuration "
            "(ENABLE_WRITE_OPERATIONS=false)."
        )
    if not s.github_write_token:
        raise SecurityGovernanceError(
            "GITHUB_WRITE_TOKEN is not configured in environment."
        )
    return s.github_write_token
