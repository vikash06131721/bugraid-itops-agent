from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class QueryIntent(str, Enum):
    INCIDENT_LOOKUP      = "incident_lookup"
    SERVICE_HEALTH       = "service_health"
    DEPLOYMENT_HISTORY   = "deployment_history"
    DEPENDENCY_ANALYSIS  = "dependency_analysis"
    PATTERN_ANALYSIS     = "pattern_analysis"
    MULTI_DOC_SYNTHESIS  = "multi_doc_synthesis"
    GAP_IDENTIFICATION   = "gap_identification"
    GENERAL              = "general"


class DataSource(str, Enum):
    OPENSEARCH = "opensearch"
    NEO4J      = "neo4j"
    MELT       = "melt"


class TimeWindow(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    description: str = ""


class QueryPlan(BaseModel):
    intent: QueryIntent
    entities: list[str] = Field(default_factory=list)
    time_window: TimeWindow | None = None
    sources_needed: list[DataSource]
    filters: dict[str, Any] = Field(default_factory=dict)
    raw_query: str
    ambiguous: bool = False


class Source(BaseModel):
    type: DataSource
    document_id: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    excerpt: str
    cypher: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    sources: list[Source] = Field(default_factory=list)
    iteration: int = 0

    @property
    def doc_ids(self) -> set[str]:
        return {s.document_id for s in self.sources}

    def new_since(self, previous_ids: set[str]) -> list[Source]:
        return [s for s in self.sources if s.document_id not in previous_ids]


class Claim(BaseModel):
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_id: str


class ResearchResponse(BaseModel):
    question_id: str
    question: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    iterations_used: int
    sources: list[Source]
    claims: list[Claim]
    knowledge_gaps: list[str]
    latency_ms: int
    token_usage: dict[str, int]
    cost_usd: float


class ResearchState(BaseModel):
    query: str
    question_id: str = "Q0"
    query_plan: QueryPlan | None = None
    evidence: Evidence = Field(default_factory=Evidence)
    seen_doc_ids: set[str] = Field(default_factory=set)
    iterations_used: int = 0
    confidence: float = 0.0
    no_new_evidence: bool = False
    answer: str = ""
    sources: list[Source] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    knowledge_gaps: list[str] = Field(default_factory=list)
    start_time_ms: int = 0
    token_usage: dict[str, int] = Field(default_factory=lambda: {"input": 0, "output": 0})
    cost_usd: float = 0.0

    model_config = {"arbitrary_types_allowed": True}
