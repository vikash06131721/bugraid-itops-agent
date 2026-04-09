"""
Layer 4 — grounded response generation.

Every claim must cite a source. We verify that after generation and flag violations.
Streams tokens as SSE so the engineer sees the answer building in real time.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from src.models import Claim, Evidence, QueryPlan, ResearchResponse, Source
from src.utils.grounding import compute_cost, validate_grounding

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are BugRaid, an ITOps research agent helping engineers debug production incidents.

You have been given a set of retrieved evidence. Your job is to synthesize it into a grounded answer.

CRITICAL RULES:
1. Every factual claim must cite its source using [source_id] notation, e.g. [INC-2024-0042]
2. If you're not certain about something, say so explicitly
3. If data is missing or you cannot find the answer in the evidence, say "I don't have data on X"
4. Never fabricate information that isn't in the provided evidence
5. Confidence below 0.6 should be flagged with "⚠️ Low confidence:"

Format your answer as:
1. A clear, direct answer paragraph (cite sources inline)
2. Key findings as bullet points (each bullet cites at least one source)
3. Knowledge gaps section: what you couldn't find or confirm

Keep the answer focused and actionable. Engineers are reading this under pressure.
"""

CLAIM_EXTRACTION_PROMPT = """Extract all factual claims from this answer as a JSON array.

For each claim:
- "claim": the factual assertion (one sentence)
- "confidence": your confidence score 0.0-1.0
- "source_id": the source ID cited for this claim (from the [brackets] in the text)

Return ONLY a JSON array, no other text.

Answer:
{answer}
"""


class ResponseGenerator:
    def __init__(self, api_key: str) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)

    async def generate_streaming(
        self,
        query: str,
        evidence: Evidence,
        query_plan: QueryPlan,
        question_id: str = "Q0",
        start_time_ms: int = 0,
    ) -> AsyncIterator[str]:
        """
        Stream the response as SSE-compatible JSON chunks.

        """
        evidence_block = self._format_evidence(evidence)

        user_message = (
            f"Question: {query}\n\n"
            f"Retrieved Evidence:\n{evidence_block}\n\n"
            "Please answer the question using only the evidence above. Cite all sources."
        )

        full_answer = ""
        input_tokens = 0
        output_tokens = 0

        with self.client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for text in stream.text_stream:
                full_answer += text
                yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"

            final_msg = stream.get_final_message()
            input_tokens = final_msg.usage.input_tokens
            output_tokens = final_msg.usage.output_tokens

        claims = await self._extract_claims(full_answer)
        knowledge_gaps = self._extract_gaps(full_answer)
        violations = validate_grounding(full_answer, evidence.sources, claims)

        if violations:
            logger.warning("Grounding violations found: %s", violations)

        latency_ms = int(time.time() * 1000) - start_time_ms if start_time_ms else 0
        cost = compute_cost(input_tokens, output_tokens)

        response = ResearchResponse(
            question_id=question_id,
            question=query,
            answer=full_answer,
            confidence=self._compute_answer_confidence(claims),
            iterations_used=evidence.iteration,
            sources=evidence.sources,
            claims=claims,
            knowledge_gaps=knowledge_gaps,
            latency_ms=latency_ms,
            token_usage={"input": input_tokens, "output": output_tokens},
            cost_usd=cost,
        )

        yield f"data: {json.dumps({'type': 'done', 'response': response.model_dump(mode='json')})}\n\n"

    async def generate(
        self,
        query: str,
        evidence: Evidence,
        query_plan: QueryPlan,
        question_id: str = "Q0",
        start_time_ms: int = 0,
    ) -> ResearchResponse:
        """Non-streaming version used by the evaluation runner and tests."""
        evidence_block = self._format_evidence(evidence)
        user_message = (
            f"Question: {query}\n\n"
            f"Retrieved Evidence:\n{evidence_block}\n\n"
            "Please answer the question using only the evidence above. Cite all sources."
        )

        response_msg = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        full_answer = response_msg.content[0].text
        input_tokens = response_msg.usage.input_tokens
        output_tokens = response_msg.usage.output_tokens

        claims = await self._extract_claims(full_answer)
        knowledge_gaps = self._extract_gaps(full_answer)
        violations = validate_grounding(full_answer, evidence.sources, claims)

        if violations:
            logger.warning("Grounding violations: %s", violations)

        latency_ms = int(time.time() * 1000) - start_time_ms if start_time_ms else 0

        return ResearchResponse(
            question_id=question_id,
            question=query,
            answer=full_answer,
            confidence=self._compute_answer_confidence(claims),
            iterations_used=evidence.iteration,
            sources=evidence.sources,
            claims=claims,
            knowledge_gaps=knowledge_gaps,
            latency_ms=latency_ms,
            token_usage={"input": input_tokens, "output": output_tokens},
            cost_usd=compute_cost(input_tokens, output_tokens),
        )

    def _format_evidence(self, evidence: Evidence) -> str:
        if not evidence.sources:
            return "No evidence retrieved."

        lines: list[str] = []
        for src in evidence.sources:
            source_type = src.type.value.upper()
            lines.append(
                f"[{src.document_id}] ({source_type}, relevance={src.relevance_score:.2f})\n"
                f"  {src.excerpt}"
            )
        return "\n\n".join(lines)

    async def _extract_claims(self, answer: str) -> list[Claim]:
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": CLAIM_EXTRACTION_PROMPT.format(answer=answer[:2000]),
                }],
            )
            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            return [
                Claim(
                    claim=item["claim"],
                    confidence=float(item.get("confidence", 0.7)),
                    source_id=item.get("source_id", "unknown"),
                )
                for item in data
                if isinstance(item, dict) and "claim" in item
            ]
        except Exception as e:
            logger.warning("Claim extraction failed: %s", e)
            return []

    def _extract_gaps(self, answer: str) -> list[str]:
        gaps: list[str] = []

        gap_markers = ["knowledge gap", "don't know", "don't have data", "missing", "unavailable", "unclear"]

        lines = answer.split("\n")
        in_gap_section = False
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            if "knowledge gap" in line_stripped.lower():
                in_gap_section = True
                continue

            if in_gap_section and line_stripped.startswith(("-", "•", "*")):
                gaps.append(line_stripped.lstrip("-•* "))
            elif in_gap_section and not line_stripped.startswith(("-", "•", "*")):
                in_gap_section = False

        for line in lines:
            if any(marker in line.lower() for marker in gap_markers) and line not in gaps:
                cleaned = line.strip().lstrip("-•* ")
                if len(cleaned) > 20:  # ignore very short fragments
                    gaps.append(cleaned)

        seen: set[str] = set()
        unique_gaps: list[str] = []
        for g in gaps:
            if g not in seen:
                seen.add(g)
                unique_gaps.append(g)

        return unique_gaps[:5]  # cap at 5 gaps

    def _compute_answer_confidence(self, claims: list[Claim]) -> float:
        if not claims:
            return 0.5
        return round(sum(c.confidence for c in claims) / len(claims), 2)
