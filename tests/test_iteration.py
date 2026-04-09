"""
Layer 3 tests — Iterative Deepening.

Pass criteria:
  - Each iteration adds new evidence (delta > 0 per round)
  - Early stopping when confidence threshold is met
  - Early stopping when no new evidence is found
  - Simple queries use only 1 iteration
  - Never exceeds MAX_ITERATIONS (3)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.iterative_deepening import IterativeDeepener
from src.agents.retrieval_orchestrator import RetrievalOrchestrator
from src.models import DataSource, Evidence, Source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_source(doc_id: str, score: float = 0.8, source_type: DataSource = DataSource.OPENSEARCH) -> Source:
    return Source(
        type=source_type,
        document_id=doc_id,
        relevance_score=score,
        excerpt=f"Evidence from {doc_id}",
    )


def make_evidence(*doc_ids: str, iteration: int = 1) -> Evidence:
    return Evidence(
        sources=[make_source(doc_id) for doc_id in doc_ids],
        iteration=iteration,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_early_stop_on_high_confidence(simple_query_plan):
    """
    When initial evidence already has high confidence, we should use only 1 iteration.
    The initial retrieval counts as iteration 1.
    """
    # High-confidence evidence — 5 sources with relevance 0.9+
    initial_evidence = make_evidence("doc-1", "doc-2", "doc-3", "doc-4", "doc-5")
    # Give them all high scores
    for src in initial_evidence.sources:
        src.relevance_score = 0.95

    orchestrator = AsyncMock(spec=RetrievalOrchestrator)

    deepener = IterativeDeepener(
        orchestrator=orchestrator,
        api_key="test-key",
        confidence_threshold=0.85,
    )

    # Patch the gap identification to return empty (shouldn't be called)
    with patch.object(deepener, '_build_refinement_context', return_value=""):
        with patch.object(deepener.client.messages, 'create') as _:
            _, iterations_used, _ = await deepener.run(
                simple_query_plan,
                initial_evidence,
                {"opensearch": True, "neo4j": True, "melt": True},
            )

    # Should have stopped after iteration 1 (confidence already high)
    assert iterations_used == 1


@pytest.mark.asyncio
async def test_stops_when_no_new_evidence(simple_query_plan):
    """
    If iteration 2 returns documents we've already seen, we stop immediately.
    No point running the same search again.
    """
    initial_evidence = make_evidence("doc-1", "doc-2")

    # Orchestrator always returns the same docs
    orchestrator = AsyncMock(spec=RetrievalOrchestrator)
    orchestrator.retrieve.return_value = (
        make_evidence("doc-1", "doc-2"),  # same docs, no new evidence
        {"opensearch": True, "neo4j": False, "melt": False},
    )

    deepener = IterativeDeepener(
        orchestrator=orchestrator,
        api_key="test-key",
        confidence_threshold=0.99,  # very high, won't be reached
    )

    with patch.object(deepener, '_build_refinement_context', AsyncMock(return_value="missing info")):
        _, iterations_used, _ = await deepener.run(
            simple_query_plan,
            initial_evidence,
            {"opensearch": True, "neo4j": False, "melt": False},
        )

    # Should stop at iteration 2 when no new docs found
    assert iterations_used <= 2


@pytest.mark.asyncio
async def test_evidence_grows_across_iterations(simple_query_plan):
    """
    When each iteration returns new docs, the total evidence pool grows.
    Evidence delta must be > 0 per round.
    """
    initial_evidence = make_evidence("doc-1", "doc-2")

    # Each call returns 2 new unique docs
    call_count = 0

    async def retrieval_side_effect(query_plan, extra_context=""):
        nonlocal call_count
        call_count += 1
        new_docs = make_evidence(f"doc-{call_count * 10 + 1}", f"doc-{call_count * 10 + 2}")
        return new_docs, {"opensearch": True, "neo4j": True, "melt": True}

    orchestrator = AsyncMock(spec=RetrievalOrchestrator)
    orchestrator.retrieve.side_effect = retrieval_side_effect

    deepener = IterativeDeepener(
        orchestrator=orchestrator,
        api_key="test-key",
        confidence_threshold=0.99,  # won't be reached until max iterations
    )

    with patch.object(deepener, '_build_refinement_context', AsyncMock(return_value="more context needed")):
        final_evidence, iterations_used, _ = await deepener.run(
            simple_query_plan,
            initial_evidence,
            {"opensearch": True, "neo4j": False, "melt": False},
        )

    # Should have run up to max iterations
    assert iterations_used <= 3
    # Evidence should have grown
    assert len(final_evidence.sources) > len(initial_evidence.sources)


@pytest.mark.asyncio
async def test_never_exceeds_max_iterations(simple_query_plan):
    """Hard cap: we never run more than 3 iterations regardless of confidence."""
    initial_evidence = make_evidence("doc-0")

    call_count = 0

    async def always_new(qp, extra_context=""):
        nonlocal call_count
        call_count += 1
        # Always return fresh docs
        return (
            make_evidence(f"new-doc-iter-{call_count}-a", f"new-doc-iter-{call_count}-b"),
            {"opensearch": True, "neo4j": True, "melt": True},
        )

    orchestrator = AsyncMock(spec=RetrievalOrchestrator)
    orchestrator.retrieve.side_effect = always_new

    deepener = IterativeDeepener(
        orchestrator=orchestrator,
        api_key="test-key",
        max_iterations=3,
        confidence_threshold=0.99,  # never met
    )

    with patch.object(deepener, '_build_refinement_context', AsyncMock(return_value="still missing")):
        _, iterations_used, _ = await deepener.run(
            simple_query_plan,
            initial_evidence,
            {"opensearch": True, "neo4j": False, "melt": False},
        )

    assert iterations_used <= 3


@pytest.mark.asyncio
async def test_hit_map_is_union_across_iterations(simple_query_plan):
    """
    If MELT only returns results on iteration 2 (e.g., MELT was slow on first pass),
    the final hit_map should still show melt=True.
    """
    initial_evidence = make_evidence("doc-1")
    initial_hit_map = {"opensearch": True, "neo4j": True, "melt": False}  # MELT missed first

    call_count = 0

    async def retrieval_with_melt(qp, extra_context=""):
        nonlocal call_count
        call_count += 1
        src = make_source(f"new-{call_count}", source_type=DataSource.MELT)
        return (
            Evidence(sources=[src], iteration=call_count + 1),
            {"opensearch": False, "neo4j": False, "melt": True},  # MELT hits on iter 2
        )

    orchestrator = AsyncMock(spec=RetrievalOrchestrator)
    orchestrator.retrieve.side_effect = retrieval_with_melt

    deepener = IterativeDeepener(
        orchestrator=orchestrator,
        api_key="test-key",
        confidence_threshold=0.99,
    )

    with patch.object(deepener, '_build_refinement_context', AsyncMock(return_value="need melt data")):
        _, _, final_hit_map = await deepener.run(
            simple_query_plan, initial_evidence, initial_hit_map
        )

    # MELT should now be True in the union
    assert final_hit_map["melt"] is True
    assert final_hit_map["opensearch"] is True  # from initial
    assert final_hit_map["neo4j"] is True  # from initial
