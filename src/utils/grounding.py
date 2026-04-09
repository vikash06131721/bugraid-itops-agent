"""
Grounding validator — every claim must cite a source in the retrieved set.
"""

from __future__ import annotations

import re

from src.models import Claim, Source


def extract_source_references(text: str) -> set[str]:
    pattern = r"\[([A-Za-z0-9\-_]+)\]"
    return set(re.findall(pattern, text))


def validate_grounding(
    answer: str,
    sources: list[Source],
    claims: list[Claim],
) -> list[str]:
    valid_ids = {s.document_id for s in sources}
    violations: list[str] = []

    for claim in claims:
        if claim.source_id not in valid_ids:
            violations.append(
                f"Claim '{claim.claim[:60]}...' cites '{claim.source_id}' "
                f"which is not in the retrieved sources."
            )

    return violations


def estimate_confidence(sources: list[Source], query_intent: str) -> float:
    """Heuristic used to decide whether to keep iterating — not the final per-claim score."""
    if not sources:
        return 0.0

    top_scores = sorted(
        (s.relevance_score for s in sources),
        reverse=True,
    )[:5]

    base_confidence = sum(top_scores) / len(top_scores)

    source_types = {s.type for s in sources}
    diversity_bonus = 0.05 * (len(source_types) - 1)
    volume_bonus = min(0.05, len(sources) * 0.005)

    return min(1.0, base_confidence + diversity_bonus + volume_bonus)


def compute_cost(input_tokens: int, output_tokens: int) -> float:
    """claude-sonnet-4-20250514: $3/M input, $15/M output."""
    input_cost = (input_tokens / 1_000_000) * 3.00
    output_cost = (output_tokens / 1_000_000) * 15.00
    return round(input_cost + output_cost, 6)
