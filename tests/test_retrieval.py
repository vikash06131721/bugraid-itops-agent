"""
Layer 2 tests — Parallel Retrieval.

Key assertion: all three sources are queried AT THE SAME TIME, not sequentially.
We verify this by timing the parallel call vs the sum of individual mock delays.

Pass criteria: timing confirms parallel execution (total time ≈ slowest source,
not sum of all sources).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.retrieval_orchestrator import RetrievalOrchestrator
from src.models import DataSource, Evidence, QueryIntent, QueryPlan, Source
from src.retrieval.melt_retriever import MELTRetriever
from src.retrieval.neo4j_retriever import Neo4jRetriever
from src.retrieval.opensearch_retriever import OpenSearchRetriever


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_source(source_type: DataSource, doc_id: str) -> Source:
    return Source(type=source_type, document_id=doc_id, relevance_score=0.8, excerpt="test excerpt")


# ---------------------------------------------------------------------------
# Parallel execution tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieval_is_parallel_not_sequential(simple_query_plan):
    """
    The most important test in this file.

    We mock each retriever to sleep for 0.3 seconds (simulating network latency).
    If retrieval is sequential, total time would be ~0.9s (3 × 0.3s).
    If retrieval is parallel, total time should be ~0.3s (the slowest source).

    We assert total time < 0.6s to confirm parallelism.
    """
    DELAY = 0.3  # seconds per source

    async def slow_opensearch(qp, extra=""):
        await asyncio.sleep(DELAY)
        return [make_source(DataSource.OPENSEARCH, "os-doc-1")]

    async def slow_neo4j(qp, extra=""):
        await asyncio.sleep(DELAY)
        return [make_source(DataSource.NEO4J, "neo4j-node-1")]

    async def slow_melt(qp, extra=""):
        await asyncio.sleep(DELAY)
        return [make_source(DataSource.MELT, "melt-metric-1")]

    # Patch the retriever methods
    os_retriever = AsyncMock(spec=OpenSearchRetriever)
    neo4j_retriever = AsyncMock(spec=Neo4jRetriever)
    melt_retriever = AsyncMock(spec=MELTRetriever)

    os_retriever.search = slow_opensearch
    neo4j_retriever.query = slow_neo4j
    melt_retriever.fetch = slow_melt

    orchestrator = RetrievalOrchestrator(os_retriever, neo4j_retriever, melt_retriever)

    start = time.perf_counter()
    evidence, hit_map = await orchestrator.retrieve(simple_query_plan)
    elapsed = time.perf_counter() - start

    # Parallel: should take ~0.3s, not ~0.9s
    # We allow up to 0.6s to account for event loop overhead
    assert elapsed < 0.6, (
        f"Retrieval took {elapsed:.2f}s — this looks sequential (expected <0.6s for parallel)"
    )

    # All three sources should have returned results
    assert len(evidence.sources) == 3
    assert hit_map["opensearch"] is True
    assert hit_map["neo4j"] is True
    assert hit_map["melt"] is True


@pytest.mark.asyncio
async def test_one_source_failure_does_not_block_others(simple_query_plan):
    """
    If OpenSearch goes down mid-query, Neo4j and MELT still return results.
    The overall query succeeds (degraded, not failed).
    """
    os_retriever = AsyncMock(spec=OpenSearchRetriever)
    neo4j_retriever = AsyncMock(spec=Neo4jRetriever)
    melt_retriever = AsyncMock(spec=MELTRetriever)

    # OpenSearch blows up
    os_retriever.search = AsyncMock(side_effect=ConnectionError("OpenSearch is down"))
    neo4j_retriever.query = AsyncMock(return_value=[make_source(DataSource.NEO4J, "neo4j-1")])
    melt_retriever.fetch = AsyncMock(return_value=[make_source(DataSource.MELT, "melt-1")])

    orchestrator = RetrievalOrchestrator(os_retriever, neo4j_retriever, melt_retriever)
    evidence, hit_map = await orchestrator.retrieve(simple_query_plan)

    # Should still have 2 sources
    assert len(evidence.sources) == 2
    assert hit_map["opensearch"] is False
    assert hit_map["neo4j"] is True
    assert hit_map["melt"] is True


@pytest.mark.asyncio
async def test_all_sources_fail_returns_empty_not_exception(simple_query_plan):
    """Even if all sources fail, we return empty evidence, not an exception."""
    os_retriever = AsyncMock(spec=OpenSearchRetriever)
    neo4j_retriever = AsyncMock(spec=Neo4jRetriever)
    melt_retriever = AsyncMock(spec=MELTRetriever)

    os_retriever.search = AsyncMock(side_effect=Exception("down"))
    neo4j_retriever.query = AsyncMock(side_effect=Exception("down"))
    melt_retriever.fetch = AsyncMock(side_effect=Exception("down"))

    orchestrator = RetrievalOrchestrator(os_retriever, neo4j_retriever, melt_retriever)
    evidence, hit_map = await orchestrator.retrieve(simple_query_plan)

    assert len(evidence.sources) == 0
    assert all(not v for v in hit_map.values())


@pytest.mark.asyncio
async def test_sources_have_provenance_tags(simple_query_plan):
    """Every retrieved source must have a type field identifying where it came from."""
    os_retriever = AsyncMock(spec=OpenSearchRetriever)
    neo4j_retriever = AsyncMock(spec=Neo4jRetriever)
    melt_retriever = AsyncMock(spec=MELTRetriever)

    os_retriever.search = AsyncMock(return_value=[make_source(DataSource.OPENSEARCH, "inc-1")])
    neo4j_retriever.query = AsyncMock(return_value=[make_source(DataSource.NEO4J, "node-1")])
    melt_retriever.fetch = AsyncMock(return_value=[make_source(DataSource.MELT, "metric-1")])

    orchestrator = RetrievalOrchestrator(os_retriever, neo4j_retriever, melt_retriever)
    evidence, _ = await orchestrator.retrieve(simple_query_plan)

    for source in evidence.sources:
        assert source.type in (DataSource.OPENSEARCH, DataSource.NEO4J, DataSource.MELT)
        assert source.document_id  # must have an ID
        assert source.excerpt       # must have an excerpt


@pytest.mark.asyncio
async def test_melt_retriever_health_snapshot(melt_retriever, simple_query_plan):
    """MELT retriever returns metrics and error logs for a health query."""
    sources = await melt_retriever.fetch(simple_query_plan)
    assert len(sources) > 0
    # All sources should have type=melt
    assert all(s.type == DataSource.MELT for s in sources)
    # Should surface the connection pool exhaustion error
    excerpts = " ".join(s.excerpt for s in sources)
    assert "payment-svc" in excerpts.lower() or "connection" in excerpts.lower()


@pytest.mark.asyncio
async def test_melt_retriever_pattern_includes_batch_logs(melt_retriever, pattern_query_plan):
    """Pattern analysis queries surface the Friday batch job logs."""
    sources = await melt_retriever.fetch(pattern_query_plan)
    excerpts = " ".join(s.excerpt for s in sources)
    # The batch job log should appear
    assert "batch" in excerpts.lower() or "reconciliation" in excerpts.lower()
