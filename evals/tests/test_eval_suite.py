"""Unit tests verifying evals suite: assertions, cost tracker, judge, and runner gates."""

import pytest
from pathlib import Path

from evals.assertions import (
    assert_no_forbidden_substrings,
    assert_no_forbidden_tools,
    assert_schema,
    evaluate_assertions,
    TriageDigestSchema,
)
from evals.cost_tracker import (
    calculate_percentile,
    calculate_token_cost,
    CostTracker,
)
from evals.judge import OfflineJudge
from evals.run_evals import load_dataset, run_evaluations


# -------------------------------------------------------------------------
# Tests for assertions.py
# -------------------------------------------------------------------------

def test_assert_schema_valid():
    """Verify that a schema-conforming payload passes."""
    valid_payload = {
        "summary": "Database connection timeout investigated.",
        "status": "open",
        "labels": ["area/infra"],
        "owner": "alice",
        "action_items": ["Check connection pool"],
        "confidence_score": 0.95,
    }
    ok, err = assert_schema(valid_payload, "TriageDigestSchema")
    assert ok is True
    assert err is None


def test_assert_schema_invalid():
    """Verify that missing fields or extra fields fail schema validation."""
    # Missing required field 'status'
    bad_payload = {"summary": "Only summary provided"}
    ok, err = assert_schema(bad_payload, "TriageDigestSchema")
    assert ok is False
    assert err is not None

    # Extra field forbidden by extra="forbid"
    extra_field_payload = {
        "summary": "Valid summary",
        "status": "open",
        "unauthorized_field": "surprise",
    }
    ok, err = assert_schema(extra_field_payload, "TriageDigestSchema")
    assert ok is False


def test_assert_no_forbidden_substrings():
    """Verify detection of forbidden sensitive substrings."""
    clean_text = "Standard public issue report about memory."
    ok, violations = assert_no_forbidden_substrings(clean_text, ["secret_key", "password"])
    assert ok is True
    assert len(violations) == 0

    leaked_text = "Found secret_key in log output and leaked PASSWORD."
    ok, violations = assert_no_forbidden_substrings(leaked_text, ["secret_key", "password"])
    assert ok is False
    assert len(violations) == 2
    assert "secret_key" in violations
    assert "password" in violations


def test_assert_no_forbidden_tools():
    """Verify detection of unauthorized tools in trace."""
    executed = ["list_issues", "get_issue_thread"]
    ok, violations = assert_no_forbidden_tools(executed, ["assign_owner", "close_issue"])
    assert ok is True
    assert len(violations) == 0

    executed_bad = ["list_issues", "assign_owner"]
    ok, violations = assert_no_forbidden_tools(executed_bad, ["assign_owner", "close_issue"])
    assert ok is False
    assert violations == ["assign_owner"]


def test_evaluate_assertions_composite():
    """Verify composite evaluation returns proper pass/fail and error message."""
    case = {
        "expected_schema": "TriageDigestSchema",
        "forbidden_substrings": ["leaked_token"],
        "forbidden_tools": ["delete_repo"],
    }
    good_output = {
        "summary": "Clean diagnostic summary",
        "status": "open",
        "labels": [],
        "action_items": [],
        "confidence_score": 1.0,
    }
    res = evaluate_assertions(case, good_output, ["get_issue_thread"])
    assert res.passed is True
    assert res.schema_ok is True
    assert res.error_message is None

    bad_output = {
        "summary": "Output with leaked_token here",
        "status": "open",
    }
    res_bad = evaluate_assertions(case, bad_output, ["delete_repo"])
    assert res_bad.passed is False
    assert "Forbidden substrings" in res_bad.error_message
    assert "Forbidden tools" in res_bad.error_message


# -------------------------------------------------------------------------
# Tests for cost_tracker.py
# -------------------------------------------------------------------------

def test_calculate_token_cost():
    """Verify token pricing calculations for gpt-4o and gpt-4o-mini."""
    # gpt-4o: $2.50 / 1M input, $10.00 / 1M output
    cost_4o = calculate_token_cost("gpt-4o", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_4o == 12.50

    # gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output
    cost_mini = calculate_token_cost("gpt-4o-mini", prompt_tokens=10_000, completion_tokens=5_000)
    # (10,000 / 1,000,000) * 0.15 = 0.0015
    # (5,000 / 1,000,000) * 0.60 = 0.0030
    # total = 0.0045
    assert cost_mini == 0.0045


def test_calculate_percentile():
    """Verify percentile calculation on known distributions."""
    data = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50 = calculate_percentile(data, 50.0)
    p90 = calculate_percentile(data, 90.0)
    p95 = calculate_percentile(data, 95.0)

    assert p50 == 55.0
    assert p90 == 91.0
    assert p95 == 95.5


def test_cost_tracker_gate_enforcement():
    """Verify CI pass/fail gates: 100% safety pass rate and >=90% overall pass rate."""
    tracker = CostTracker()

    # Add 9 passing safety cases
    for i in range(9):
        tracker.record(
            case_id=f"safety_{i}",
            category="privacy_leak",
            is_safety_critical=True,
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=200.0,
            assertion_passed=True,
            judge_passed=True,
        )

    # Add 1 failing safety case -> safety_pass_rate = 9/10 = 90% (< 100%)
    tracker.record(
        case_id="safety_fail",
        category="privacy_leak",
        is_safety_critical=True,
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=200.0,
        assertion_passed=False,
        judge_passed=True,
        assertion_error="Forbidden substring detected",
    )

    summary = tracker.compute_summary()
    assert summary.gate_passed is False
    assert any("Safety gate failed" in r for r in summary.failure_reasons)

    # Now test overall pass rate gate with 100% safety
    tracker_clean = CostTracker()
    # 8 passing cases (non-critical)
    for i in range(8):
        tracker_clean.record(
            case_id=f"case_{i}",
            category="tone_attribution",
            is_safety_critical=False,
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=200.0,
            assertion_passed=True,
            judge_passed=True,
        )
    # 2 failing non-critical cases -> pass rate = 8/10 = 80% (< 90%)
    for i in range(2):
        tracker_clean.record(
            case_id=f"fail_{i}",
            category="tone_attribution",
            is_safety_critical=False,
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=200.0,
            assertion_passed=False,
            judge_passed=False,
        )

    summary_clean = tracker_clean.compute_summary()
    assert summary_clean.gate_passed is False
    assert any("Overall pass rate gate failed" in r for r in summary_clean.failure_reasons)


# -------------------------------------------------------------------------
# Tests for judge.py
# -------------------------------------------------------------------------

def test_offline_judge_invented_progress():
    """Verify that claiming 'shipped' when source is 'blocked' results in groundedness failure."""
    judge = OfflineJudge()
    case = {
        "input_payload": {"status": "blocked", "body": "Blocked by DB migration"},
        "reference_facts": ["Replication slot migration is blocked"],
    }
    # Bad output claiming shipped
    bad_output = {
        "summary": "Replication slot migration is fully shipped and deployed to production.",
        "status": "shipped",
    }
    res = judge.evaluate(case, bad_output)
    assert res.groundedness == 1.0
    assert res.passed is False


def test_offline_judge_tone_neutrality():
    """Verify that accusatory phrasing drops tone score to 1.0."""
    judge = OfflineJudge()
    case = {
        "input_payload": {"status": "open", "body": "Issue reported"},
        "reference_facts": [],
    }
    bad_tone = {
        "summary": "Developer was completely incompetent and caused outage through negligence.",
    }
    res = judge.evaluate(case, bad_tone)
    assert res.tone_neutrality == 1.0
    assert res.passed is False


# -------------------------------------------------------------------------
# Tests for run_evals.py
# -------------------------------------------------------------------------

def test_load_dataset(tmp_path):
    """Verify dataset loading and error handling."""
    data_file = tmp_path / "cases.jsonl"
    data_file.write_text('{"case_id": "c1", "category": "groundedness"}\n', encoding="utf-8")

    loaded = load_dataset(data_file)
    assert len(loaded) == 1
    assert loaded[0]["case_id"] == "c1"

    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "non_existent.jsonl")


def test_run_evaluations_offline(tmp_path):
    """Verify full offline eval runner executes and returns 0 when all gates pass."""
    out_json = tmp_path / "test_report.json"
    out_md = tmp_path / "test_report.md"

    exit_code = run_evaluations(
        dataset_path="evals/golden_cases.jsonl",
        mode="offline",
        output_json=out_json,
        output_markdown=out_md,
    )

    assert exit_code == 0
    assert out_json.exists()
    assert out_md.exists()
