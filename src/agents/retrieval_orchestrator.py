"""
Layer 2 — parallel retrieval across all three data sources.

asyncio.gather fires OpenSearch, Neo4j, and MELT simultaneously.
If one source fails, the other two still return — return_exceptions=True
handles that without crashing the whole request.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.models import DataSource, Evidence, QueryPlan, Source
from src.retrieval.melt_retriever import MELTRetriever
from src.retrieval.neo4j_retriever import Neo4jRetriever
from src.retrieval.opensearch_retriever import OpenSearchRetriever

logger = logging.getLogger(__name__)


class RetrievalOrchestrator:
    def __init__(
        self,
        opensearch: OpenSearchRetriever,
        neo4j: Neo4jRetriever,
        melt: MELTRetriever,
    ) -> None:
        self.opensearch = opensearch
        self.neo4j = neo4j
        self.melt = melt

    async def retrieve(
        self,
        query_plan: QueryPlan,
        extra_context: str = "",
    ) -> tuple[Evidence, dict[str, bool]]:
        """
        Query all three sources in parallel.

        """
        start = time.perf_counter()

        results = await asyncio.gather(
            self._safe_opensearch(query_plan, extra_context),
            self._safe_neo4j(query_plan, extra_context),
            self._safe_melt(query_plan, extra_context),
            return_exceptions=True,
        )

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.info("Parallel retrieval completed in %dms", elapsed_ms)

        os_results, neo4j_results, melt_results = [
            r if isinstance(r, list) else []
            for r in results
        ]

        hit_map = {
            "opensearch": len(os_results) > 0,
            "neo4j": len(neo4j_results) > 0,
            "melt": len(melt_results) > 0,
        }
        logger.info("Source hit map: %s", hit_map)

        all_sources: list[Source] = os_results + neo4j_results + melt_results
        return Evidence(sources=all_sources), hit_map

    async def _safe_opensearch(self, query_plan: QueryPlan, extra: str) -> list[Source]:
        try:
            t0 = time.perf_counter()
            results = await self.opensearch.search(query_plan, extra)
            logger.debug("OpenSearch returned %d results in %dms", len(results), int((time.perf_counter() - t0) * 1000))
            return results
        except Exception as e:
            logger.warning("OpenSearch retrieval failed: %s — continuing without it", e)
            return []

    async def _safe_neo4j(self, query_plan: QueryPlan, extra: str) -> list[Source]:
        try:
            t0 = time.perf_counter()
            results = await self.neo4j.query(query_plan, extra)
            logger.debug("Neo4j returned %d results in %dms", len(results), int((time.perf_counter() - t0) * 1000))
            return results
        except Exception as e:
            logger.warning("Neo4j retrieval failed: %s — continuing without it", e)
            return []

    async def _safe_melt(self, query_plan: QueryPlan, extra: str) -> list[Source]:
        try:
            t0 = time.perf_counter()
            results = await self.melt.fetch(query_plan, extra)
            logger.debug("MELT returned %d results in %dms", len(results), int((time.perf_counter() - t0) * 1000))
            return results
        except Exception as e:
            logger.warning("MELT retrieval failed: %s — continuing without it", e)
            return []
