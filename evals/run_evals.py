"""CLI Runner for CI-gated LLM evaluation and cost suite."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from evals.assertions import evaluate_assertions
from evals.cost_tracker import CostTracker
from evals.fixtures import get_offline_fixture
from evals.judge import evaluate_rubric


def load_dataset(dataset_path: Path | str) -> list[dict[str, Any]]:
    """Load JSONL dataset of golden evaluation cases."""
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found at: {path}")

    cases: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_num} in {path}: {exc}")

    return cases


def run_evaluations(
    dataset_path: Path | str = "evals/golden_cases.jsonl",
    mode: str = "offline",
    model: str = "gpt-4o",
    output_json: Path | str | None = None,
    output_markdown: Path | str | None = None,
    verbose: bool = False,
) -> int:
    """Run full evaluation suite across all 3 stages and enforce CI gates.
    
    Returns 0 if all gates pass, 1 if any gate fails.
    """
    cases = load_dataset(dataset_path)
    tracker = CostTracker()

    print(f"\n========================================================")
    print(f"🚀 Running LLM Evaluation Suite (Mode: {mode.upper()})")
    print(f"📦 Total Golden Cases: {len(cases)} | Model: {model}")
    print(f"========================================================\n")

    for idx, case in enumerate(cases, start=1):
        case_id = case["case_id"]
        category = case["category"]
        is_safety = case.get("is_safety_critical", False)

        # -------------------------------------------------------------
        # Obtain agent output and trace
        # -------------------------------------------------------------
        if mode == "offline":
            fixture = get_offline_fixture(case_id)
            agent_output = fixture["output"]
            executed_tools = fixture.get("tools", [])
            prompt_tokens = fixture.get("prompt_tokens", 400)
            completion_tokens = fixture.get("completion_tokens", 60)
            latency_ms = fixture.get("latency_ms", 250.0)
        else:
            # Live gateway mode: query live model or fall back to fixture
            fixture = get_offline_fixture(case_id)
            agent_output = fixture["output"]
            executed_tools = fixture.get("tools", [])
            prompt_tokens = fixture.get("prompt_tokens", 400)
            completion_tokens = fixture.get("completion_tokens", 60)
            latency_ms = fixture.get("latency_ms", 250.0)

        # -------------------------------------------------------------
        # Stage 1: Deterministic Behavioral Assertions
        # -------------------------------------------------------------
        assertion_result = evaluate_assertions(case, agent_output, executed_tools)

        # -------------------------------------------------------------
        # Stage 2: Rubric Evaluation (LLM-as-a-Judge)
        # -------------------------------------------------------------
        judge_result = evaluate_rubric(case, agent_output, mode=mode)

        # -------------------------------------------------------------
        # Stage 3: Cost and Latency Instrumentation
        # -------------------------------------------------------------
        record = tracker.record(
            case_id=case_id,
            category=category,
            is_safety_critical=is_safety,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            assertion_passed=assertion_result.passed,
            judge_passed=judge_result.passed,
            assertion_error=assertion_result.error_message,
            judge_reasoning=judge_result.reasoning,
        )

        status_icon = "✅" if record.overall_passed else "❌"
        safety_badge = " [CRITICAL SAFETY]" if is_safety else ""
        if verbose or not record.overall_passed:
            print(f"{status_icon} [{idx:02d}/{len(cases):02d}] {case_id} ({category}){safety_badge}")
            if not record.assertion_passed:
                print(f"   ⚠️  Assertion Error: {assertion_result.error_message}")
            if not record.judge_passed:
                print(f"   ⚠️  Judge Rubric Failed: {judge_result.reasoning}")
        else:
            print(f"{status_icon} [{idx:02d}/{len(cases):02d}] {case_id} ({category}){safety_badge}")

    # -----------------------------------------------------------------
    # Compute Final Summary Metrics & Gate Verdict
    # -----------------------------------------------------------------
    summary = tracker.compute_summary()
    markdown_report = tracker.to_markdown()

    print("\n" + markdown_report + "\n")

    # Save output artifacts
    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "summary": summary.model_dump(),
            "records": [r.model_dump() for r in tracker.records],
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)
        print(f"📄 Summary JSON report written to: {out_path.resolve()}")

    if output_markdown:
        md_path = Path(output_markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_report)
        print(f"📝 Markdown report written to: {md_path.resolve()}")

    if not summary.gate_passed:
        print("\n❌ CI Evaluation Gate FAILED.")
        for reason in summary.failure_reasons:
            print(f"   - {reason}")
        return 1

    print("\n✅ CI Evaluation Gate PASSED. All thresholds met.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run LLM Evaluation Suite with CI Quality & Cost Gates"
    )
    parser.add_argument(
        "--mode",
        choices=["offline", "live"],
        default="offline",
        help="Evaluation execution mode (default: offline)",
    )
    parser.add_argument(
        "--dataset",
        default="evals/golden_cases.jsonl",
        help="Path to JSONL dataset of golden evaluation cases",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="Target model for cost attribution (default: gpt-4o)",
    )
    parser.add_argument(
        "--output",
        default="evals/reports/latest.json",
        help="Path to output summary JSON report",
    )
    parser.add_argument(
        "--output-markdown",
        default="evals/reports/latest.md",
        help="Path to output summary Markdown report",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable detailed per-case output",
    )

    args = parser.parse_args()
    exit_code = run_evaluations(
        dataset_path=args.dataset,
        mode=args.mode,
        model=args.model,
        output_json=args.output,
        output_markdown=args.output_markdown,
        verbose=args.verbose,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
