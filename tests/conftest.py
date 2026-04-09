"""
Shared test fixtures for the BugRaid test suite.

All fixtures here are available to every test file without importing.
The fixtures provide:
  - Mock clients for OpenSearch and Neo4j (so tests run without Docker)
  - Sample data (QueryPlan, Evidence, Source objects)
  - A pre-loaded MELTRetriever with minimal in-memory data
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import (
    Claim,
    DataSource,
    Evidence,
    QueryIntent,
    QueryPlan,
    Source,
    TimeWindow,
)
from src.retrieval.melt_retriever import MELTRetriever


# ---------------------------------------------------------------------------
# Sample QueryPlan fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_query_plan() -> QueryPlan:
    """A straightforward service health check query."""
    return QueryPlan(
        intent=QueryIntent.SERVICE_HEALTH,
        entities=["payment-svc"],
        time_window=None,
        sources_needed=[DataSource.OPENSEARCH, DataSource.NEO4J, DataSource.MELT],
        filters={},
        raw_query="Is payment-svc healthy right now?",
    )


@pytest.fixture
def dependency_query_plan() -> QueryPlan:
    return QueryPlan(
        intent=QueryIntent.DEPENDENCY_ANALYSIS,
        entities=["checkout-svc"],
        time_window=None,
        sources_needed=[DataSource.NEO4J],
        filters={},
        raw_query="Which services does checkout-svc depend on?",
    )


@pytest.fixture
def deployment_query_plan() -> QueryPlan:
    return QueryPlan(
        intent=QueryIntent.DEPLOYMENT_HISTORY,
        entities=[],
        time_window=TimeWindow(
            start=datetime(2024, 11, 13, 0, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 11, 14, 0, 0, 0, tzinfo=timezone.utc),
            description="last 24 hours",
        ),
        sources_needed=[DataSource.NEO4J],
        filters={},
        raw_query="What deployments happened in the last 24 hours?",
    )


@pytest.fixture
def pattern_query_plan() -> QueryPlan:
    return QueryPlan(
        intent=QueryIntent.PATTERN_ANALYSIS,
        entities=["checkout-svc"],
        time_window=None,
        sources_needed=[DataSource.OPENSEARCH, DataSource.NEO4J, DataSource.MELT],
        filters={},
        raw_query="Why does checkout slow down every Friday evening?",
    )


# ---------------------------------------------------------------------------
# Sample Source and Evidence fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def opensearch_source() -> Source:
    return Source(
        type=DataSource.OPENSEARCH,
        document_id="INC-2024-0487",
        relevance_score=0.92,
        excerpt=(
            "Payment service latency spike — checkout and gateway cascade. "
            "Memory leak in payment-svc connection pool caused gradual exhaustion."
        ),
        metadata={"service": "payment-svc", "severity": "P1"},
    )


@pytest.fixture
def neo4j_source() -> Source:
    return Source(
        type=DataSource.NEO4J,
        document_id="neo4j-deps-checkout-svc",
        relevance_score=0.95,
        excerpt="checkout-svc depends on: payment-svc (critical, 45ms), auth-svc (critical, 20ms)",
        cypher="MATCH (s:Service {name: 'checkout-svc'})-[r:DEPENDS_ON]->(dep) RETURN dep",
        metadata={"service": "checkout-svc"},
    )


@pytest.fixture
def melt_source() -> Source:
    return Source(
        type=DataSource.MELT,
        document_id="melt-log-payment-svc-2024-11-12T14:00:00Z",
        relevance_score=0.90,
        excerpt="[ERROR] payment-svc @ 2024-11-12T14:00:00Z: connection_pool_exhaustion",
        metadata={"service": "payment-svc", "level": "ERROR"},
    )


@pytest.fixture
def sample_evidence(opensearch_source, neo4j_source, melt_source) -> Evidence:
    return Evidence(
        sources=[opensearch_source, neo4j_source, melt_source],
        iteration=1,
    )


@pytest.fixture
def sample_claims(opensearch_source) -> list[Claim]:
    return [
        Claim(claim="payment-svc experienced connection pool exhaustion", confidence=0.95, source_id="INC-2024-0487"),
        Claim(claim="checkout-svc depends on payment-svc", confidence=0.99, source_id="neo4j-deps-checkout-svc"),
    ]


# ---------------------------------------------------------------------------
# Mock OpenSearch client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_opensearch_client():
    """An AsyncOpenSearch mock that returns realistic-looking hits."""
    client = AsyncMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "INC-2024-0487",
                    "_score": 0.92,
                    "_source": {
                        "incident_id": "INC-2024-0487",
                        "title": "Payment service latency spike — checkout cascade",
                        "severity": "P1",
                        "service": "payment-svc",
                        "timestamp": "2024-11-12T14:00:00Z",
                        "rca_summary": "Memory leak in connection pool",
                        "root_cause": "connection_pool_exhaustion",
                        "resolution": "Restarted pods. Applied connection pool patch.",
                        "tags": ["memory", "cascade", "P1"],
                    },
                }
            ]
        }
    }
    client.indices.exists.return_value = False
    return client


# ---------------------------------------------------------------------------
# Mock Neo4j driver
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_neo4j_driver():
    """A Neo4j AsyncDriver mock that returns dependency results."""

    async def mock_run(cypher, params=None):
        """Fake cursor that yields one record."""
        record = MagicMock()
        record.__aiter__ = AsyncMock(return_value=iter([]))
        return record

    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock(
        __aiter__=AsyncMock(return_value=iter([
            {"dependency": "payment-svc", "tier": 1, "team": "payments", "latency_ms": 45, "criticality": "critical"},
            {"dependency": "auth-svc",    "tier": 1, "team": "identity", "latency_ms": 20, "criticality": "critical"},
        ]))
    ))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    driver = AsyncMock()
    driver.session.return_value = session
    return driver


# ---------------------------------------------------------------------------
# MELT retriever with minimal in-memory data
# ---------------------------------------------------------------------------

@pytest.fixture
def melt_retriever(tmp_path) -> MELTRetriever:
    """A real MELTRetriever loaded with minimal test data."""
    melt_data = {
        "window": {"start": "2024-11-07T00:00:00Z", "end": "2024-11-13T23:59:59Z"},
        "metrics": [
            {"timestamp": "2024-11-12T14:00:00Z", "service": "payment-svc",  "name": "connection_pool_utilization", "value": 100.0, "unit": "%"},
            {"timestamp": "2024-11-12T14:10:00Z", "service": "checkout-svc", "name": "p99_latency_ms",             "value": 2200.0, "unit": "ms"},
            {"timestamp": "2024-11-08T18:05:00Z", "service": "checkout-svc", "name": "p99_latency_ms",             "value": 287.0,  "unit": "ms"},
            {"timestamp": "2024-11-13T23:00:00Z", "service": "payment-svc",  "name": "memory_used_percent",        "value": 68.0,   "unit": "%"},
        ],
        "logs": [
            {"timestamp": "2024-11-12T14:00:00Z", "service": "payment-svc",  "level": "ERROR", "message": "connection_pool_exhaustion: all connections in use", "trace_id": "t-0100"},
            {"timestamp": "2024-11-12T14:20:00Z", "service": "gateway-svc",  "level": "ERROR", "message": "Retry storm detected. Rate: 0.87", "trace_id": "t-0120"},
            {"timestamp": "2024-11-08T18:00:00Z", "service": "inventory-svc","level": "INFO",  "message": "weekly_batch_reconciliation started. Table locks acquired.", "trace_id": "t-0300"},
            {"timestamp": "2024-11-08T18:05:00Z", "service": "checkout-svc", "level": "WARN",  "message": "Elevated latency on inventory lookups. DB wait: 380ms", "trace_id": "t-0301"},
        ],
        "traces": [
            {"trace_id": "trace-cascade-001", "service": "payment-svc",  "operation": "get_connection", "duration_ms": 5010, "status": "error", "timestamp": "2024-11-12T14:10:32Z"},
            {"trace_id": "trace-friday-001",  "service": "checkout-svc", "operation": "check_inventory","duration_ms": 412,  "status": "slow",  "timestamp": "2024-11-08T18:10:00Z"},
        ],
    }

    data_file = tmp_path / "melt_telemetry.json"
    data_file.write_text(json.dumps(melt_data))

    retriever = MELTRetriever(data_file)
    retriever.load()
    return retriever
