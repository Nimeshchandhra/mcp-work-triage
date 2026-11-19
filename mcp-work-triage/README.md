# Least-Privilege Python MCP Server (`mcp-work-triage`)

An open-source Model Context Protocol (MCP) server providing **least-privilege GitHub Issue Triage** with **structural governance** and **inversion thinking**. Built using the official MCP Python SDK (`mcp[cli]`).

---

## Architecture & Threat Model

Traditional agent architectures grant broad read-write API keys and rely on system prompt instructions (e.g., *"Do not close issues or assign tickets without permission"*) to maintain safety. **System prompts are not security boundaries.** Adversarial user prompts, jailbreaks, or hallucinations can bypass prompt-based guardrails.

`mcp-work-triage` implements **Inversion Thinking** and **Structural Governance**:

```
+-----------------------------------------------------------------------------------+
|                                  AI AGENT                                        |
+--------------------+------------------------------+-------------------------------+
                     |                              |
            (1) Read-Only Query            (2) Stage Draft
                     v                              v
+------------------------------------+  +-------------------------------------------+
|      Tool: list_issues /           |  |       Tool: draft_triage_update           |
|      Tool: get_issue_thread        |  |                                           |
|                                    |  | - Rejects commitment keys in Pydantic     |
| - Read-Only HTTP GET only          |  |   (assignee, milestone, due_date, close)  |
| - Strips private / internal notes  |  | - Writes to local staging SQLite DB       |
| - Credential: GITHUB_READ_TOKEN    |  | - Status: pending_human_review            |
+------------------------------------+  | - Credential: Local DB only (Zero GitHub) |
                                        +-------------------------------------------+
                                                            |
                                                            v
                                                [ Human Operator Review ]
                                                            | (Approves draft &
                                                            |  generates secret token)
                                                            v
                                        +-------------------------------------------+
                                        |       Tool: commit_approved_draft         |
                                        |                                           |
                                        | - Requires verified approval token        |
                                        | - Credential: GITHUB_WRITE_TOKEN (scoped) |
                                        | - Applies labels & comments ONLY          |
                                        | - Master switch ENABLE_WRITE_OPERATIONS   |
                                        +-------------------------------------------+
                                                            |
                                                            v
                                        +-------------------------------------------+
                                        |           GitHub API (Labels/Comments)    |
                                        +-------------------------------------------+
```

### Threat Vectors & Structural Mitigations

| Threat Vector | Prompt-Based Defense (Flawed) | Structural Defense (`mcp-work-triage`) |
| :--- | :--- | :--- |
| **Autonomous Ticket Assignment / Closing** | *"Please do not assign yourself or close issues."* | **Zero Code Path:** No tools or API endpoints exist for `assign_owner`, `set_due_date`, or `close_issue`. Pydantic models forbid these fields with `extra="forbid"`. |
| **Credential Hijacking / Token Scope Escalation** | Single token with read/write repo access. | **Credential Scoping Isolation:** `GITHUB_READ_TOKEN` and `GITHUB_WRITE_TOKEN` are physically isolated. Read tools never receive the write token. |
| **Unapproved Agent Write Actions** | Hope the LLM asks before taking actions. | **Human-in-the-Loop Gating:** Drafts are staged locally in SQLite with `status='pending_human_review'`. Commits require a cryptographically verified `human_approval_token`. |
| **Accidental or Malicious Re-Execution** | Idempotency ignored; duplicate comments created. | **Deterministic Idempotency:** Every mutation generates a SHA-256 idempotency key (`draft_id + target_issue`), preventing duplicate writes on retries. |
| **Private Information Leakage** | Ask LLM to ignore internal comments. | **Programmatic Sanitization:** Regex filters strip internal markers (e.g. `<!-- private: ... -->`) before returning data to the model. |

---

## Tool Inventory & Risk Tiers

| Tool Name | Risk Tier | Credential Scope | Allowed Behavior | Structural Restriction |
| :--- | :--- | :--- | :--- | :--- |
| `list_issues` | `READ_ONLY` | Read-only PAT (`public_repo` / `issues:read`) | List open issues with pagination & filters | Read-only HTTP `GET` only |
| `get_issue_thread` | `READ_ONLY` | Read-only PAT | Fetch full issue body + comments | Strips any private/internal markers if configured |
| `draft_triage_update` | `HUMAN_GATED_DRAFT` | Local staging DB only (no GitHub write token) | Writes proposed label/summary to local `drafts` table with `status="pending_human_review"` | Physically lacks GitHub write credentials |
| `commit_approved_draft` | `HUMAN_GATED_WRITE` | Scoped write PAT (optional, disabled by default) | Executes an approved draft **only** if `human_approval_token` matches a verified local approval record | Rejects any direct call without prior human approval; **never** exposes `assignee`, `due_date`, or `close` parameters |

---

## API Failure Modes & Resilience Engineering

The server implements production-grade resilience against real-world distributed failure modes:

1. **Rate Limiting (`429 Too Many Requests` & `403 X-RateLimit-Remaining: 0`):**
   - Parses `Retry-After` header or calculates delta from epoch `X-RateLimit-Reset`.
   - Executes jittered exponential backoff up to `max_retries=3`.
   - Raises structured `RateLimitExceededError` with retry guidance if limits persist.

2. **Pagination Drift & Cyclic Links:**
   - Follows GitHub RFC-5988 `Link: <...>; rel="next"` pagination headers.
   - Enforces a hard `max_pages` safety cap (default `10`) to eliminate infinite pagination loops.
   - Maintains a visited URL set to detect and abort on cyclic pagination links.

3. **Timeouts & Connection Failures:**
   - Explicit connect timeout (`5.0s`) and read timeout (`15.0s`) configured on `httpx.AsyncClient`.
   - Transient network connection failures retry automatically with backoff.

4. **Transient 5xx Server Errors:**
   - GitHub 500, 502, 503, and 504 errors trigger exponential backoff retries.

5. **Write Idempotency:**
   - Uses deterministic `X-Idempotency-Key` headers (`sha256(draft_id + repo#issue)`).
   - In-memory and staging DB status tracking guarantees that approved drafts cannot be submitted multiple times.

---

## Audit Logging

Every tool invocation emits an immutable, structured record to `audit_log.jsonl`:

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp_utc": "2026-11-17T18:00:00.000000+00:00",
  "tool_name": "draft_triage_update",
  "risk_tier": "HUMAN_GATED_DRAFT",
  "credential_scope": "local_db_only",
  "args_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "outcome": "allowed",
  "latency_ms": 14.2,
  "error_type": null
}
```

- **Zero PII / Zero Secret Leakage:** Tool arguments are hashed via deterministic canonical SHA-256 (`args_sha256`).
- **Complete Auditability:** Every execution is reconstructable by security teams.

---

## Setup & Quickstart

### 1. Installation

Using `uv`:

```bash
cd mcp-work-triage
uv pip install -e ".[dev]"
```

Or using standard `pip`:

```bash
pip install -e ".[dev]"
```

### 2. Environment Configuration

Copy the example environment configuration:

```bash
cp .env.example .env
```

Configure your scoped tokens in `.env`:

```ini
# Read-only token (public_repo or issues:read)
GITHUB_READ_TOKEN=ghp_your_read_token

# Scoped write token (only needed if human-approved commits are enabled)
GITHUB_WRITE_TOKEN=ghp_your_write_token

# Master switch: Keep false unless actively committing approved drafts
ENABLE_WRITE_OPERATIONS=false
```

### 3. Running Verification Tests

```bash
uv run pytest -v
```

### 4. Running with MCP Inspector

Inspect and interact with tool schemas using `mcp dev`:

```bash
uv run mcp dev src/mcp_work_triage/server.py
```
