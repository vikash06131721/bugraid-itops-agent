"""
Layer 1 tests — Query Understanding Agent.

Tests that the agent correctly parses all 10 test questions into the right
intent + entity + source combinations.

Pass criteria: 10/10 queries parsed correctly (right intent, right entities,
ambiguous queries handled without exceptions).
"""

from __future__ import annotations

from datetime import timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.query_understanding import QueryUnderstandingAgent
from src.models import DataSource, QueryIntent


# We test the _parse_response method directly to avoid making real API calls.
# The integration test (test_evaluation.py) tests the full pipeline.
@pytest.fixture
def agent() -> QueryUnderstandingAgent:
    return QueryUnderstandingAgent(api_key="test-key")


# ---------------------------------------------------------------------------
# Direct parsing tests — we give it raw JSON (as Claude would return)
# ---------------------------------------------------------------------------

def test_parse_service_health(agent):
    raw = """{
        "intent": "service_health",
        "entities": ["payment-svc"],
        "time_window": null,
        "sources_needed": ["opensearch", "neo4j", "melt"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "Is payment-svc healthy right now?")
    assert plan.intent == QueryIntent.SERVICE_HEALTH
    assert "payment-svc" in plan.entities
    assert DataSource.MELT in plan.sources_needed


def test_parse_deployment_history(agent):
    raw = """{
        "intent": "deployment_history",
        "entities": [],
        "time_window": {"start": "2024-11-13T00:00:00Z", "end": "2024-11-14T00:00:00Z", "description": "last 24 hours"},
        "sources_needed": ["neo4j"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "What deployments happened in the last 24 hours?")
    assert plan.intent == QueryIntent.DEPLOYMENT_HISTORY
    assert plan.time_window is not None
    assert plan.time_window.start is not None
    assert plan.time_window.start.tzinfo is not None  # must be timezone-aware
    assert DataSource.NEO4J in plan.sources_needed


def test_parse_dependency_analysis(agent):
    raw = """{
        "intent": "dependency_analysis",
        "entities": ["checkout-svc"],
        "time_window": null,
        "sources_needed": ["neo4j"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "Which services does checkout-svc depend on?")
    assert plan.intent == QueryIntent.DEPENDENCY_ANALYSIS
    assert "checkout-svc" in plan.entities


def test_parse_incident_lookup(agent):
    raw = """{
        "intent": "incident_lookup",
        "entities": ["auth-svc"],
        "time_window": {"start": "2024-10-01T00:00:00Z", "end": "2024-10-31T23:59:59Z", "description": "last month"},
        "sources_needed": ["opensearch", "neo4j"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "What incidents involved auth-svc last month?")
    assert plan.intent == QueryIntent.INCIDENT_LOOKUP
    assert "auth-svc" in plan.entities
    assert DataSource.OPENSEARCH in plan.sources_needed


def test_parse_pattern_analysis(agent):
    raw = """{
        "intent": "pattern_analysis",
        "entities": ["checkout-svc"],
        "time_window": null,
        "sources_needed": ["opensearch", "neo4j", "melt"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "Why does checkout slow down every Friday evening?")
    assert plan.intent == QueryIntent.PATTERN_ANALYSIS
    assert "checkout-svc" in plan.entities
    # Pattern queries need MELT for temporal signals
    assert DataSource.MELT in plan.sources_needed


def test_parse_multi_doc_synthesis(agent):
    raw = """{
        "intent": "multi_doc_synthesis",
        "entities": ["payment-svc"],
        "time_window": null,
        "sources_needed": ["opensearch", "neo4j"],
        "filters": {},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "Compare how we resolved the last 5 payment incidents")
    assert plan.intent == QueryIntent.MULTI_DOC_SYNTHESIS


def test_parse_gap_identification(agent):
    raw = """{
        "intent": "gap_identification",
        "entities": [],
        "time_window": {"start": "2024-11-14T00:00:00Z", "end": "2024-11-14T23:59:59Z", "description": "today"},
        "sources_needed": ["opensearch", "neo4j", "melt"],
        "filters": {},
        "ambiguous": true
    }"""
    plan = agent._parse_response(raw, "What don't we know about today's incident?")
    assert plan.intent == QueryIntent.GAP_IDENTIFICATION
    # Gap queries are inherently ambiguous
    assert plan.ambiguous is True


def test_parse_ambiguous_query_no_exception(agent):
    """Ambiguous queries must be handled gracefully — never throw."""
    raw = """{
        "intent": "general",
        "entities": ["checkout-svc"],
        "time_window": null,
        "sources_needed": ["opensearch", "neo4j", "melt"],
        "filters": {},
        "ambiguous": true
    }"""
    # This should not raise
    plan = agent._parse_response(raw, "why is checkout slow")
    assert plan is not None
    assert plan.raw_query == "why is checkout slow"


def test_fallback_on_malformed_json(agent):
    """If Claude returns garbage, we fall back to a safe default plan."""
    plan = agent._parse_response("this is not json at all", "some query")
    # Fallback plan uses GENERAL intent and all sources
    assert plan.intent == QueryIntent.GENERAL
    assert DataSource.OPENSEARCH in plan.sources_needed
    assert DataSource.NEO4J in plan.sources_needed
    assert DataSource.MELT in plan.sources_needed
    # Should never raise
    assert plan.ambiguous is True


def test_fallback_extracts_known_services(agent):
    """The fallback plan still tries to find known service names in the query."""
    plan = agent._fallback_plan("Is payment-svc and checkout-svc having issues?")
    assert "payment-svc" in plan.entities
    assert "checkout-svc" in plan.entities


def test_parse_all_sources_for_complex_query(agent):
    """Complex cross-source queries should request all three sources."""
    raw = """{
        "intent": "incident_lookup",
        "entities": ["payment-svc"],
        "time_window": null,
        "sources_needed": ["opensearch", "neo4j", "melt"],
        "filters": {"severity": "P1"},
        "ambiguous": false
    }"""
    plan = agent._parse_response(raw, "What is the payment service responsible for?")
    assert len(plan.sources_needed) >= 2  # must use at least 2 sources
