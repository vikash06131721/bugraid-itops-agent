"""
Layer 3 — iterative deepening.

Each round uses the evidence found so far to guide a smarter follow-up search.
We stop early if confidence is high enough, no new documents come back, or we
hit the max iteration cap.
"""

from __future__ import annotations

import logging

import anthropic

from src.agents.retrieval_orchestrator import RetrievalOrchestrator
from src.models import Evidence, QueryPlan
from src.utils.grounding import estimate_confidence

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3
CONFIDENCE_THRESHOLD = 0.85


class IterativeDeepener:
    def __init__(
        self,
        orchestrator: RetrievalOrchestrator,
        api_key: str,
        max_iterations: int = MAX_ITERATIONS,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self.orchestrator = orchestrator
        self.client = anthropic.Anthropic(api_key=api_key)
        self.max_iterations = max_iterations
        self.confidence_threshold = confidence_threshold

    async def run(
        self,
        query_plan: QueryPlan,
        initial_evidence: Evidence,
        initial_hit_map: dict[str, bool],
        max_iterations_override: int | None = None,
    ) -> tuple[Evidence, int, dict[str, bool]]:
        evidence = Evidence(sources=initial_evidence.sources, iteration=1)
        seen_ids = set(evidence.doc_ids)
        hit_map = dict(initial_hit_map)
        iterations_used = 1
        cap = max_iterations_override if max_iterations_override is not None else self.max_iterations

        confidence = estimate_confidence(evidence.sources, query_plan.intent.value)
        logger.info("After iteration 1: %d sources, confidence=%.2f", len(evidence.sources), confidence)

        for i in range(2, cap + 1):
            if confidence >= self.confidence_threshold:
                logger.info("Confidence %.2f >= threshold — stopping at iteration %d", confidence, i - 1)
                break

            refined_context = await self._build_refinement_context(query_plan, evidence)
            new_evidence, new_hits = await self.orchestrator.retrieve(query_plan, extra_context=refined_context)

            genuinely_new = [s for s in new_evidence.sources if s.document_id not in seen_ids]

            if not genuinely_new:
                logger.info("Iteration %d found 0 new documents — stopping early", i)
                break

            logger.info("Iteration %d: found %d new sources", i, len(genuinely_new))
            seen_ids.update(s.document_id for s in genuinely_new)

            evidence = Evidence(
                sources=evidence.sources + genuinely_new,
                iteration=i,
            )
            iterations_used = i

            for source, hit in new_hits.items():
                hit_map[source] = hit_map.get(source, False) or hit

            confidence = estimate_confidence(evidence.sources, query_plan.intent.value)
            logger.info("After iteration %d: %d total sources, confidence=%.2f", i, len(evidence.sources), confidence)

        return evidence, iterations_used, hit_map

    async def _build_refinement_context(self, query_plan: QueryPlan, evidence: Evidence) -> str:
        """Ask Claude what's still missing — feeds the next retrieval round."""
        if not evidence.sources:
            return ""

        excerpt_summary = "\n".join(
            f"- [{s.document_id}] {s.excerpt[:120]}"
            for s in evidence.sources[:8]  # don't overwhelm the context
        )

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system="You are helping refine a search query. Be concise.",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Original question: {query_plan.raw_query}\n\n"
                        f"Evidence found so far:\n{excerpt_summary}\n\n"
                        "In one sentence, what specific information is still missing to fully answer the question?"
                    ),
                }],
            )
            gap = response.content[0].text.strip()
            logger.debug("Identified gap for iteration: %s", gap)
            return gap

        except Exception as e:
            logger.warning("Gap identification failed: %s — using empty refinement", e)
            return ""
