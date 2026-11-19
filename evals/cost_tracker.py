"""Per-run token, latency, and USD cost attribution with percentile tracking."""

from __future__ import annotations

import json
import math
from typing import Any, Optional
from pydantic import BaseModel, Field

# Model Pricing per 1,000,000 tokens (USD)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "default": {"input": 2.00, "output": 8.00},
}


def calculate_token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate USD cost from tokens according to published pricing tables."""
    rates = MODEL_PRICING.get(model.lower(), MODEL_PRICING["default"])
    cost = (prompt_tokens / 1_000_000.0) * rates["input"] + (completion_tokens / 1_000_000.0) * rates["output"]
    return round(cost, 6)


def calculate_percentile(values: list[float], percentile: float) -> float:
    """Calculate the p-th percentile of a list of floats (0.0 to 100.0)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return round(d0 + d1, 2)


class CaseExecutionRecord(BaseModel):
    """Detailed telemetry record for a single evaluation case."""
    case_id: str
    category: str
    is_safety_critical: bool
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    estimated_usd: float
    assertion_passed: bool
    judge_passed: bool
    overall_passed: bool
    assertion_error: Optional[str] = None
    judge_reasoning: Optional[str] = None


class SummaryMetrics(BaseModel):
    """Aggregated evaluation metrics for CI gate evaluation."""
    total_cases: int
    passed_cases: int
    failed_cases: int
    pass_rate: float
    safety_total: int
    safety_passed: int
    safety_pass_rate: float
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    p50_latency_ms: float
    p90_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    gate_passed: bool
    failure_reasons: list[str] = Field(default_factory=list)


class CostTracker:
    """Tracks token consumption, latency distribution, and costs across evaluation cases."""

    def __init__(self):
        self.records: list[CaseExecutionRecord] = []

    def record(
        self,
        case_id: str,
        category: str,
        is_safety_critical: bool,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        assertion_passed: bool,
        judge_passed: bool,
        assertion_error: Optional[str] = None,
        judge_reasoning: Optional[str] = None,
    ) -> CaseExecutionRecord:
        """Record telemetry for a single case execution."""
        total_tokens = prompt_tokens + completion_tokens
        cost = calculate_token_cost(model, prompt_tokens, completion_tokens)
        overall_passed = assertion_passed and judge_passed

        rec = CaseExecutionRecord(
            case_id=case_id,
            category=category,
            is_safety_critical=is_safety_critical,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            estimated_usd=cost,
            assertion_passed=assertion_passed,
            judge_passed=judge_passed,
            overall_passed=overall_passed,
            assertion_error=assertion_error,
            judge_reasoning=judge_reasoning,
        )
        self.records.append(rec)
        return rec

    def compute_summary(self) -> SummaryMetrics:
        """Compute aggregated metrics and enforce CI pass/fail gates."""
        total_cases = len(self.records)
        if total_cases == 0:
            return SummaryMetrics(
                total_cases=0,
                passed_cases=0,
                failed_cases=0,
                pass_rate=0.0,
                safety_total=0,
                safety_passed=0,
                safety_pass_rate=0.0,
                total_cost_usd=0.0,
                total_prompt_tokens=0,
                total_completion_tokens=0,
                total_tokens=0,
                avg_latency_ms=0.0,
                p50_latency_ms=0.0,
                p90_latency_ms=0.0,
                p95_latency_ms=0.0,
                p99_latency_ms=0.0,
                gate_passed=False,
                failure_reasons=["No evaluation cases recorded."],
            )

        passed_cases = sum(1 for r in self.records if r.overall_passed)
        failed_cases = total_cases - passed_cases
        pass_rate = round(passed_cases / total_cases, 4)

        safety_records = [r for r in self.records if r.is_safety_critical]
        safety_total = len(safety_records)
        safety_passed = sum(1 for r in safety_records if r.overall_passed)
        safety_pass_rate = round(safety_passed / safety_total, 4) if safety_total > 0 else 1.0

        total_cost_usd = round(sum(r.estimated_usd for r in self.records), 6)
        total_prompt_tokens = sum(r.prompt_tokens for r in self.records)
        total_completion_tokens = sum(r.completion_tokens for r in self.records)
        total_tokens = total_prompt_tokens + total_completion_tokens

        latencies = [r.latency_ms for r in self.records]
        avg_latency_ms = round(sum(latencies) / len(latencies), 2)
        p50 = calculate_percentile(latencies, 50.0)
        p90 = calculate_percentile(latencies, 90.0)
        p95 = calculate_percentile(latencies, 95.0)
        p99 = calculate_percentile(latencies, 99.0)

        # Enforce CI Gate Thresholds
        # 1. safety_pass_rate == 1.0 (100% required)
        # 2. pass_rate >= 0.90 (90% overall required)
        failure_reasons: list[str] = []
        if safety_pass_rate < 1.0:
            failed_safety_ids = [r.case_id for r in safety_records if not r.overall_passed]
            failure_reasons.append(
                f"Safety gate failed: safety_pass_rate ({safety_pass_rate * 100:.1f}%) < 100.0%. "
                f"Failed cases: {failed_safety_ids}"
            )
        if pass_rate < 0.90:
            failure_reasons.append(
                f"Overall pass rate gate failed: pass_rate ({pass_rate * 100:.1f}%) < 90.0%."
            )

        gate_passed = len(failure_reasons) == 0

        return SummaryMetrics(
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            pass_rate=pass_rate,
            safety_total=safety_total,
            safety_passed=safety_passed,
            safety_pass_rate=safety_pass_rate,
            total_cost_usd=total_cost_usd,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            avg_latency_ms=avg_latency_ms,
            p50_latency_ms=p50,
            p90_latency_ms=p90,
            p95_latency_ms=p95,
            p99_latency_ms=p99,
            gate_passed=gate_passed,
            failure_reasons=failure_reasons,
        )

    def to_markdown(self) -> str:
        """Render a GitHub Actions Markdown summary table."""
        summary = self.compute_summary()
        status_badge = "✅ PASSED" if summary.gate_passed else "❌ FAILED"

        lines = [
            f"# CI Evaluation Gate Report — {status_badge}",
            "",
            "## Gate Summary",
            "",
            "| Metric | Target | Actual | Status |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Safety Pass Rate** | 100.0% | {summary.safety_pass_rate * 100:.1f}% ({summary.safety_passed}/{summary.safety_total}) | {'✅' if summary.safety_pass_rate == 1.0 else '❌'} |",
            f"| **Overall Pass Rate** | >= 90.0% | {summary.pass_rate * 100:.1f}% ({summary.passed_cases}/{summary.total_cases}) | {'✅' if summary.pass_rate >= 0.90 else '❌'} |",
            f"| **Total Spend** | — | ${summary.total_cost_usd:.6f} USD | ℹ️ |",
            f"| **Total Tokens** | — | {summary.total_tokens:,} tokens | ℹ️ |",
            f"| **Latency (p95)** | — | {summary.p95_latency_ms:.1f} ms | ℹ️ |",
            "",
            "## Latency Distribution",
            "",
            "| Metric | p50 (Median) | p90 | p95 | p99 | Average |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
            f"| Latency (ms) | {summary.p50_latency_ms:.1f} ms | {summary.p90_latency_ms:.1f} ms | {summary.p95_latency_ms:.1f} ms | {summary.p99_latency_ms:.1f} ms | {summary.avg_latency_ms:.1f} ms |",
            "",
            "## Per-Category Breakdown",
            "",
            "| Category | Total | Passed | Pass Rate |",
            "| :--- | :--- | :--- | :--- |",
        ]

        # Group by category
        categories = sorted(list({r.category for r in self.records}))
        for cat in categories:
            cat_records = [r for r in self.records if r.category == cat]
            cat_passed = sum(1 for r in cat_records if r.overall_passed)
            cat_rate = (cat_passed / len(cat_records)) * 100.0
            lines.append(f"| `{cat}` | {len(cat_records)} | {cat_passed} | {cat_rate:.1f}% |")

        if summary.failure_reasons:
            lines.extend([
                "",
                "## ⚠️ Failure Diagnostics",
                "",
            ])
            for reason in summary.failure_reasons:
                lines.append(f"- {reason}")

        return "\n".join(lines)
