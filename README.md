# Supabase AI Platform Engineer Proof-of-Work

This repository contains two production-grade, public, resume-ready engineering artifacts built to the specification in [BUILD.md](file:///C:/Users/nimes/Desktop/PROFU-pro/BUILD.md):

1. **Least-Privilege Python MCP Server (`mcp-work-triage/`)**
   - Built with the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp[cli]`).
   - Implements **Inversion Thinking** and **Structural Governance**: Agents can read issues and stage local drafts, but autonomous writes/commitments (assigning owners, setting due dates, closing issues, merging) are **structurally impossible** in code and credential scoping.
   - Resilient HTTP client with jittered exponential backoff on `429` (Rate Limiting), `403` (`X-RateLimit-Remaining: 0`), and transient `5xx` errors.
   - Pagination drift protection with hard `max_pages` cap and cycle prevention.
   - Deterministic SHA-256 idempotency keys preventing duplicate operations.
   - Thread-safe, append-only structured audit logging (`audit_log.jsonl`) recording `run_id`, `timestamp_utc`, `tool_name`, `risk_tier`, `credential_scope`, `args_sha256`, `outcome`, `latency_ms`.

2. **CI-Gated LLM Evaluation & Cost Suite (`evals/` + `.github/workflows/eval-gate.yml`)**
   - Golden evaluation dataset (`evals/golden_cases.jsonl`) containing 30 realistic cases covering all 5 failure modes:
     1. **Invented Progress:** Blocks agent from claiming shipped when status is blocked/pending.
     2. **Private-to-Public Leak:** Blocks leaking confidential notes, salaries, tokens, or PII.
     3. **Accusatory Tone / Misattributed Credit:** Enforces blameless, objective attribution.
     4. **Prompt Injection Write Attempt:** Blocks adversarial instructions aiming to invoke unauthorized mutating tools.
     5. **Malformed Schema:** Validates edge-case inputs (Unicode, emojis, empty fields, massive log dumps).
   - 3-Stage Evaluation Pipeline:
     - **Stage 1 (Deterministic Behavioral Assertions):** Pydantic schema validation, zero forbidden substrings, zero forbidden tools.
     - **Stage 2 (LLM-as-a-Judge Rubric):** 1–5 scoring across `groundedness`, `completeness`, and `tone_neutrality` (requiring `>= 4.0`).
     - **Stage 3 (Cost & Latency Instrumentation):** Per-case token, latency, and USD cost attribution with p50/p90/p95/p99 percentiles.
   - Hard Quality Gates: Exits non-zero if `safety_pass_rate < 100%` or overall `pass_rate < 90%`.
   - CI Workflow (`.github/workflows/eval-gate.yml`) running on pull requests and pushes to `main`.

---

## Directory Structure

```text
.
├── BUILD.md
├── README.md
├── pyproject.toml
├── .gitignore
├── .github/
│   └── workflows/
│       └── eval-gate.yml              # CI workflow blocking PR merge on regression
├── mcp-work-triage/
│   ├── pyproject.toml
│   ├── .env.example
│   ├── README.md                      # Architecture, threat model, API failure modes, setup
│   ├── src/
│   │   └── mcp_work_triage/
│   │       ├── __init__.py
│   │       ├── server.py              # MCPServer entrypoint & tool registration
│   │       ├── config.py              # Scoped credentials & tier configuration
│   │       ├── github_client.py       # Resilient HTTP client (429 backoff, pagination, timeouts)
│   │       ├── staging_db.py          # SQLite local staging table for human-gated drafts
│   │       ├── audit.py               # Append-only JSONL decision & tool-call audit log
│   │       └── models.py              # Strict Pydantic input/output schemas
│   └── tests/
│       ├── __init__.py
│       ├── test_governance.py         # Structural write impossibility & credential scoping
│       ├── test_api_failures.py       # 429 Retry-After, 5xx, timeouts, cursor pagination
│       └── test_audit_log.py          # Logged and reconstructable audit trails
└── evals/
    ├── __init__.py
    ├── golden_cases.jsonl             # 30 golden test cases (happy paths + adversarial)
    ├── assertions.py                  # Fast deterministic behavioral & safety checks
    ├── judge.py                       # LLM-as-a-judge rubric scorer (1-5 scale)
    ├── cost_tracker.py                # Token, latency, and USD cost attribution
    ├── fixtures.py                    # Recorded trace fixtures for offline evaluation
    ├── run_evals.py                   # CLI runner outputting summary JSON + exit code 0/1
    ├── reports/
    │   ├── sample_report.json         # Committed sample eval report
    │   └── latest.json                # Latest generated report (gitignored)
    └── tests/
        ├── __init__.py
        └── test_eval_suite.py         # Unit tests for assertions, judge, cost tracker
```

---

## Verification & Execution Guide

### 1. Run All Pytest Suites (Server + Evals)

```bash
# Run all tests across the repository
pytest -v
```

Output:
- 15 tests passed in `mcp-work-triage/tests/` (Governance, API Failures, Audit Logging)
- 12 tests passed in `evals/tests/` (Assertions, Rubric, Cost Tracker, Runner)
- Total: **27 passed**

### 2. Run the MCP Server Tests Standalone

```bash
cd mcp-work-triage
uv run pytest -v
```

### 3. Verify MCP Server Tool Schemas

```bash
uv run python -c "from mcp_work_triage.server import server; import asyncio; tools = asyncio.run(server.list_tools()); print([(t.name, list(t.input_schema.get('properties', {}).keys())) for t in tools])"
```

Registered tools:
- `list_issues`: `['repo', 'state', 'labels', 'max_pages']` (READ_ONLY)
- `get_issue_thread`: `['repo', 'issue_number']` (READ_ONLY)
- `draft_triage_update`: `['repo', 'issue_number', 'suggested_labels', 'triage_summary', 'suggested_comment']` (HUMAN_GATED_DRAFT)
- `commit_approved_draft`: `['draft_id', 'human_approval_token']` (HUMAN_GATED_WRITE)

### 4. Run the CI Evaluation Gate (Offline Mode)

```bash
python -m evals.run_evals --mode=offline --output evals/reports/latest.json --output-markdown evals/reports/latest.md
```

Summary:
- **Safety Pass Rate:** 100.0% (18/18 critical cases passed)
- **Overall Pass Rate:** 100.0% (30/30 cases passed)
- **CI Gate Verdict:** `PASSED` (Exit Code 0)
