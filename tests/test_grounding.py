"""
Layer 4 tests — Grounding validation.

Pass criteria: 0 claims without a source reference.
Every factual assertion in the generated answer must cite a real retrieved source.
"""

from __future__ import annotations

import pytest

from src.models import Claim, DataSource, Source
from src.utils.grounding import (
    compute_cost,
    estimate_confidence,
    extract_source_references,
    validate_grounding,
)


# ---------------------------------------------------------------------------
# Source reference extraction
# ---------------------------------------------------------------------------

def test_extract_source_references_basic():
    answer = "payment-svc had a memory leak [INC-2024-0487] which caused [neo4j-health-payment-svc] to degrade."
    refs = extract_source_references(answer)
    assert "INC-2024-0487" in refs
    assert "neo4j-health-payment-svc" in refs


def test_extract_source_references_multiple():
    answer = "[INC-2024-0001] shows the root cause. [melt-log-payment] confirms the timing. [neo4j-deps-checkout] shows the dependency."
    refs = extract_source_references(answer)
    assert len(refs) == 3


def test_extract_source_references_empty_answer():
    refs = extract_source_references("No sources cited here.")
    assert len(refs) == 0


def test_extract_melt_references():
    answer = "Telemetry shows a spike [melt-metric-payment-svc-p99_latency_ms] at 14:00."
    refs = extract_source_references(answer)
    assert "melt-metric-payment-svc-p99_latency_ms" in refs


# ---------------------------------------------------------------------------
# Grounding validation
# ---------------------------------------------------------------------------

def make_source(doc_id: str) -> Source:
    return Source(
        type=DataSource.OPENSEARCH,
        document_id=doc_id,
        relevance_score=0.9,
        excerpt="test",
    )


def make_claim(text: str, source_id: str, confidence: float = 0.85) -> Claim:
    return Claim(claim=text, confidence=confidence, source_id=source_id)


def test_fully_grounded_answer_has_no_violations():
    sources = [make_source("INC-2024-0487"), make_source("neo4j-deps-checkout")]
    claims = [
        make_claim("payment-svc had connection pool exhaustion", "INC-2024-0487"),
        make_claim("checkout-svc depends on payment-svc", "neo4j-deps-checkout"),
    ]
    violations = validate_grounding("any answer text", sources, claims)
    assert violations == []


def test_claim_with_unknown_source_is_a_violation():
    sources = [make_source("INC-2024-0487")]
    claims = [
        make_claim("checkout-svc depends on payment-svc", "neo4j-DOES-NOT-EXIST"),
    ]
    violations = validate_grounding("any answer", sources, claims)
    assert len(violations) == 1
    assert "neo4j-DOES-NOT-EXIST" in violations[0]


def test_zero_claims_means_zero_violations():
    sources = [make_source("INC-2024-0487")]
    violations = validate_grounding("some text", sources, [])
    assert violations == []


def test_multiple_violations_all_reported():
    sources = [make_source("real-source-1")]
    claims = [
        make_claim("claim one", "fake-source-1"),
        make_claim("claim two", "fake-source-2"),
        make_claim("valid claim", "real-source-1"),  # this one is fine
    ]
    violations = validate_grounding("answer", sources, claims)
    assert len(violations) == 2


# ---------------------------------------------------------------------------
# Confidence estimation
# ---------------------------------------------------------------------------

def test_confidence_zero_for_empty_sources():
    conf = estimate_confidence([], "incident_lookup")
    assert conf == 0.0


def test_confidence_higher_with_more_sources():
    single_source = [make_source("doc-1")]
    single_source[0].relevance_score = 0.8

    many_sources = [
        Source(type=DataSource.OPENSEARCH, document_id=f"doc-{i}", relevance_score=0.8, excerpt="x")
        for i in range(10)
    ]

    conf_single = estimate_confidence(single_source, "incident_lookup")
    conf_many = estimate_confidence(many_sources, "incident_lookup")
    assert conf_many >= conf_single


def test_confidence_boost_from_source_diversity():
    """Having all 3 source types should give a higher confidence than 1 type."""
    single_type = [
        Source(type=DataSource.OPENSEARCH, document_id=f"os-{i}", relevance_score=0.8, excerpt="x")
        for i in range(5)
    ]

    mixed_types = [
        Source(type=DataSource.OPENSEARCH, document_id="os-1", relevance_score=0.8, excerpt="x"),
        Source(type=DataSource.NEO4J,      document_id="n4j-1", relevance_score=0.8, excerpt="x"),
        Source(type=DataSource.MELT,       document_id="melt-1", relevance_score=0.8, excerpt="x"),
    ]

    conf_single_type = estimate_confidence(single_type, "service_health")
    conf_mixed = estimate_confidence(mixed_types, "service_health")
    assert conf_mixed >= conf_single_type


def test_confidence_capped_at_1():
    """Confidence should never exceed 1.0 regardless of how much evidence we have."""
    sources = [
        Source(type=DataSource.OPENSEARCH, document_id=f"doc-{i}", relevance_score=1.0, excerpt="x")
        for i in range(100)
    ]
    conf = estimate_confidence(sources, "general")
    assert conf <= 1.0


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def test_cost_is_non_negative():
    cost = compute_cost(1000, 500)
    assert cost >= 0.0


def test_cost_scales_with_tokens():
    small_cost = compute_cost(100, 50)
    large_cost = compute_cost(10000, 5000)
    assert large_cost > small_cost


def test_output_tokens_cost_more_than_input():
    """Output tokens are 5x more expensive than input for Claude Sonnet."""
    input_only_cost  = compute_cost(1_000_000, 0)
    output_only_cost = compute_cost(0, 1_000_000)
    assert output_only_cost > input_only_cost
