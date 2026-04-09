"""
In-memory metrics tracker. Fine for a single process — swap for Redis/Prometheus in prod.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _QueryRecord:
    latency_ms: int
    iterations: int
    confidence: float
    cost_usd: float
    opensearch_hit: bool
    neo4j_hit: bool
    melt_hit: bool
    hallucination: bool = False


class MetricsTracker:

    _instance: MetricsTracker | None = None
    _lock: Lock = Lock()

    def __new__(cls) -> "MetricsTracker":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._records: list[_QueryRecord] = []
                    cls._instance._write_lock = Lock()
        return cls._instance

    def record(
        self,
        latency_ms: int,
        iterations: int,
        confidence: float,
        cost_usd: float,
        opensearch_hit: bool,
        neo4j_hit: bool,
        melt_hit: bool,
        hallucination: bool = False,
    ) -> None:
        with self._write_lock:
            self._records.append(
                _QueryRecord(
                    latency_ms=latency_ms,
                    iterations=iterations,
                    confidence=confidence,
                    cost_usd=cost_usd,
                    opensearch_hit=opensearch_hit,
                    neo4j_hit=neo4j_hit,
                    melt_hit=melt_hit,
                    hallucination=hallucination,
                )
            )

    def snapshot(self) -> dict:
        with self._write_lock:
            records = list(self._records)

        if not records:
            return {
                "query_latency_p50_ms": 0,
                "query_latency_p95_ms": 0,
                "avg_iterations_per_query": 0.0,
                "source_hit_rate": {"opensearch": 0.0, "neo4j": 0.0, "melt": 0.0},
                "avg_confidence_score": 0.0,
                "avg_cost_per_query_usd": 0.0,
                "total_queries": 0,
                "hallucination_rate": 0.0,
            }

        latencies = sorted(r.latency_ms for r in records)
        n = len(records)

        def percentile(sorted_list: list[int], p: float) -> int:
            idx = max(0, int(p / 100 * len(sorted_list)) - 1)
            return sorted_list[idx]

        return {
            "query_latency_p50_ms": percentile(latencies, 50),
            "query_latency_p95_ms": percentile(latencies, 95),
            "avg_iterations_per_query": round(sum(r.iterations for r in records) / n, 2),
            "source_hit_rate": {
                "opensearch": round(sum(1 for r in records if r.opensearch_hit) / n, 2),
                "neo4j": round(sum(1 for r in records if r.neo4j_hit) / n, 2),
                "melt": round(sum(1 for r in records if r.melt_hit) / n, 2),
            },
            "avg_confidence_score": round(sum(r.confidence for r in records) / n, 2),
            "avg_cost_per_query_usd": round(sum(r.cost_usd for r in records) / n, 4),
            "total_queries": n,
            "hallucination_rate": round(sum(1 for r in records if r.hallucination) / n, 4),
        }


tracker = MetricsTracker()


def now_ms() -> int:
    return int(time.time() * 1000)
