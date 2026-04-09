"""
OpenSearch retriever — hybrid dense + BM25 search over incident documents.

BM25 handles exact keyword matches ("connection_pool_exhaustion"), dense vectors
catch semantic similarity ("memory leak" ≈ "OOM error"). RRF merges the two lists.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

import numpy as np
from opensearchpy import AsyncOpenSearch

from src.models import DataSource, QueryPlan, Source

logger = logging.getLogger(__name__)

INDEX_NAME = "bugraid-incidents"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K = 10   # how many results to return per query


@lru_cache(maxsize=1)
def _load_model():
    # fastembed uses ONNX runtime — avoids PyTorch dependency entirely
    from fastembed import TextEmbedding
    logger.info("Loading embedding model %s via fastembed...", EMBEDDING_MODEL)
    return TextEmbedding(EMBEDDING_MODEL)


def embed(text: str) -> list[float]:
    model = _load_model()
    return list(next(iter(model.embed([text]))))


def embed_batch(texts: list[str]) -> list[list[float]]:
    model = _load_model()
    return [list(v) for v in model.embed(texts)]


class OpenSearchRetriever:

    def __init__(self, client: AsyncOpenSearch) -> None:
        self.client = client

    async def search(self, query_plan: QueryPlan, extra_query: str = "") -> list[Source]:
        search_text = self._build_search_text(query_plan, extra_query)

        try:
            bm25_hits, dense_hits = await asyncio.gather(
                self._bm25_search(search_text, query_plan),
                self._dense_search(search_text, query_plan),
            )
            merged = self._reciprocal_rank_fusion(bm25_hits, dense_hits)
            return merged[:TOP_K]

        except Exception as e:
            logger.warning("OpenSearch search failed: %s", e)
            return []

    async def _bm25_search(self, text: str, query_plan: QueryPlan) -> list[dict[str, Any]]:
        filters = self._build_filters(query_plan)
        body: dict[str, Any] = {
            "size": TOP_K * 2,  # fetch more before merging
            "query": {
                "bool": {
                    "must": [{"multi_match": {"query": text, "fields": ["title^2", "rca_summary", "root_cause", "tags"]}}],
                    "filter": filters,
                }
            },
        }
        response = await self.client.search(index=INDEX_NAME, body=body)
        return response["hits"]["hits"]

    async def _dense_search(self, text: str, query_plan: QueryPlan) -> list[dict[str, Any]]:
        vector = await asyncio.get_event_loop().run_in_executor(None, embed, text)
        filters = self._build_filters(query_plan)

        body: dict[str, Any] = {
            "size": TOP_K * 2,
            "query": {
                "bool": {
                    "must": [{"knn": {"embedding": {"vector": vector, "k": TOP_K * 2}}}],
                    "filter": filters,
                }
            },
        }
        response = await self.client.search(index=INDEX_NAME, body=body)
        return response["hits"]["hits"]

    def _build_search_text(self, query_plan: QueryPlan, extra_query: str) -> str:
        parts = [query_plan.raw_query]
        if extra_query:
            parts.append(extra_query)
        if query_plan.entities:
            parts.append(" ".join(query_plan.entities))
        return " ".join(parts)

    def _build_filters(self, query_plan: QueryPlan) -> list[dict]:
        filters: list[dict] = []

        if query_plan.entities:
            service_names = [e for e in query_plan.entities if "-svc" in e]
            if service_names:
                filters.append({"terms": {"service": service_names}})

        if query_plan.time_window:
            time_filter: dict[str, Any] = {"range": {"timestamp": {}}}
            if query_plan.time_window.start:
                time_filter["range"]["timestamp"]["gte"] = query_plan.time_window.start.isoformat()
            if query_plan.time_window.end:
                time_filter["range"]["timestamp"]["lte"] = query_plan.time_window.end.isoformat()
            if time_filter["range"]["timestamp"]:
                filters.append(time_filter)

        if "severity" in query_plan.filters:
            filters.append({"term": {"severity": query_plan.filters["severity"]}})

        return filters

    def _reciprocal_rank_fusion(
        self,
        bm25_hits: list[dict],
        dense_hits: list[dict],
        k: int = 60,
    ) -> list[Source]:
        """
        RRF combines two ranked lists into one.

        For each document, RRF score = 1/(k + rank_in_bm25) + 1/(k + rank_in_dense).
        Documents appearing near the top of BOTH lists get the highest combined score.
        k=60 is the standard RRF constant that makes the merge smooth.
        """
        scores: dict[str, float] = {}
        docs: dict[str, dict] = {}

        for rank, hit in enumerate(bm25_hits, start=1):
            doc_id = hit["_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            docs[doc_id] = hit

        for rank, hit in enumerate(dense_hits, start=1):
            doc_id = hit["_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            docs[doc_id] = hit

        sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)

        results: list[Source] = []
        for doc_id in sorted_ids:
            hit = docs[doc_id]
            src = hit["_source"]
            normalized = min(1.0, scores[doc_id] * 30)
            results.append(
                Source(
                    type=DataSource.OPENSEARCH,
                    document_id=doc_id,
                    relevance_score=round(normalized, 3),
                    excerpt=self._make_excerpt(src),
                    metadata={
                        "service": src.get("service", ""),
                        "severity": src.get("severity", ""),
                        "timestamp": src.get("timestamp", ""),
                        "title": src.get("title", ""),
                        "root_cause": src.get("root_cause", ""),
                        "resolution": src.get("resolution", ""),
                    },
                )
            )

        return results

    def _make_excerpt(self, src: dict) -> str:
        parts = []
        if src.get("title"):
            parts.append(src["title"])
        if src.get("rca_summary"):
            parts.append(src["rca_summary"][:200])
        elif src.get("root_cause"):
            parts.append(f"Root cause: {src['root_cause']}")
        return " — ".join(parts) if parts else "No excerpt available"


async def make_opensearch_client(host: str = "localhost", port: int = 9200) -> AsyncOpenSearch:
    return AsyncOpenSearch(
        hosts=[{"host": host, "port": port}],
        http_compress=True,
        use_ssl=False,
        verify_certs=False,
        timeout=30,
    )
