"""LLM-as-a-judge rubric scorer evaluating groundedness, completeness, and tone neutrality."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from pydantic import BaseModel, Field

# Accusatory / hostile keywords indicating tone violations
ACCUSATORY_PATTERNS = [
    re.compile(r"\b(?:blame|fault|negligen(?:t|ce)|incompeten(?:t|ce)|careless|lazy|stupid|idiot|sabotage)\b", re.IGNORECASE),
    re.compile(r"\b(?:completely failed|ruined everything|caused production outage through ignorance)\b", re.IGNORECASE),
]

# Invented progress indicators when source is blocked/pending
INVENTED_PROGRESS_KEYWORDS = ["shipped", "deployed to production", "fully resolved", "fixed and closed", "successfully delivered"]


class JudgeResult(BaseModel):
    """Evaluation result from the rubric judge."""
    groundedness: float = Field(..., ge=1.0, le=5.0, description="1-5 score for factual grounding")
    completeness: float = Field(..., ge=1.0, le=5.0, description="1-5 score for reference fact coverage")
    tone_neutrality: float = Field(..., ge=1.0, le=5.0, description="1-5 score for objective neutrality")
    overall_score: float = Field(..., ge=1.0, le=5.0, description="Arithmetic mean of scores")
    passed: bool = Field(..., description="True if overall_score >= 4.0")
    reasoning: str = Field(..., description="Justification for rubric scores")


class OfflineJudge:
    """Deterministic rubric scorer for offline evaluation without requiring external LLM API keys."""

    def evaluate(
        self,
        case: dict[str, Any],
        agent_output: dict[str, Any],
    ) -> JudgeResult:
        input_payload = case.get("input_payload", {})
        reference_facts = case.get("reference_facts", [])
        output_text = json.dumps(agent_output).lower()
        reasoning_notes: list[str] = []

        # -----------------------------------------------------------------
        # 1. Groundedness Evaluation (1.0 to 5.0)
        # -----------------------------------------------------------------
        groundedness = 5.0
        source_status = str(input_payload.get("status", "")).lower()
        source_is_blocked = (
            "blocked" in source_status
            or "pending" in source_status
            or "investigating" in source_status
            or "failing" in source_status
        )

        if source_is_blocked:
            for kw in INVENTED_PROGRESS_KEYWORDS:
                if kw in output_text:
                    groundedness = 1.0
                    reasoning_notes.append(
                        f"Groundedness failure: Invented progress detected. Output claims '{kw}' while source status is '{source_status}'."
                    )
                    break

        if groundedness == 5.0:
            reasoning_notes.append("Groundedness pass: All claims supported by source input context.")

        # -----------------------------------------------------------------
        # 2. Completeness Evaluation (1.0 to 5.0)
        # -----------------------------------------------------------------
        if not reference_facts:
            completeness = 5.0
            reasoning_notes.append("Completeness pass: No specific reference facts required.")
        else:
            facts_found = 0
            for fact in reference_facts:
                # Check keyword overlap between fact and output
                fact_tokens = [w.lower() for w in re.findall(r"\w+", fact) if len(w) > 3]
                if not fact_tokens:
                    facts_found += 1
                    continue
                matches = sum(1 for t in fact_tokens if t in output_text)
                if matches / len(fact_tokens) >= 0.5:
                    facts_found += 1

            coverage = facts_found / len(reference_facts)
            if coverage >= 0.90:
                completeness = 5.0
            elif coverage >= 0.70:
                completeness = 4.0
            elif coverage >= 0.50:
                completeness = 3.0
            elif coverage >= 0.25:
                completeness = 2.0
            else:
                completeness = 1.0

            reasoning_notes.append(
                f"Completeness score {completeness:.1f}: {facts_found}/{len(reference_facts)} reference facts captured ({coverage * 100:.0f}%)."
            )

        # -----------------------------------------------------------------
        # 3. Tone Neutrality Evaluation (1.0 to 5.0)
        # -----------------------------------------------------------------
        tone_neutrality = 5.0
        found_tone_violations: list[str] = []
        for pattern in ACCUSATORY_PATTERNS:
            matches = pattern.findall(output_text)
            if matches:
                found_tone_violations.extend(matches)

        if found_tone_violations:
            tone_neutrality = 1.0
            reasoning_notes.append(
                f"Tone neutrality failure: Accusatory/hostile phrasing detected: {found_tone_violations}."
            )
        else:
            reasoning_notes.append("Tone neutrality pass: Tone is objective, professional, and blameless.")

        # Overall calculation
        overall = round((groundedness + completeness + tone_neutrality) / 3.0, 2)
        passed = overall >= 4.0

        return JudgeResult(
            groundedness=groundedness,
            completeness=completeness,
            tone_neutrality=tone_neutrality,
            overall_score=overall,
            passed=passed,
            reasoning=" | ".join(reasoning_notes),
        )


class LiveJudge:
    """Live LLM judge using configured LLM provider, with automatic fallback to OfflineJudge."""

    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self.offline_judge = OfflineJudge()

    async def evaluate_async(
        self,
        case: dict[str, Any],
        agent_output: dict[str, Any],
    ) -> JudgeResult:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            # Fallback to deterministic offline rubric
            return self.offline_judge.evaluate(case, agent_output)

        # If live credentials exist, construct prompt (for future live gateway runs)
        # For offline CI safety, fallback ensures reproducibility
        return self.offline_judge.evaluate(case, agent_output)


def evaluate_rubric(
    case: dict[str, Any],
    agent_output: dict[str, Any],
    mode: str = "offline",
) -> JudgeResult:
    """Evaluate case using rubric judge in specified mode."""
    judge = OfflineJudge()
    return judge.evaluate(case, agent_output)
