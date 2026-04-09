"""
FastAPI app — POST /query streams the research pipeline, GET /metrics returns aggregate stats.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import asyncio

from src.agents.iterative_deepening import IterativeDeepener
from src.agents.query_understanding import QueryUnderstandingAgent
from src.agents.response_generator import ResponseGenerator
from src.agents.retrieval_orchestrator import RetrievalOrchestrator
from src.models import DataSource, Evidence, QueryIntent, QueryPlan
from src.retrieval.melt_retriever import MELTRetriever
from src.retrieval.neo4j_retriever import Neo4jRetriever, make_neo4j_driver
from src.retrieval.opensearch_retriever import OpenSearchRetriever, make_opensearch_client
from src.utils.metrics import now_ms, tracker

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_orchestrator: RetrievalOrchestrator | None = None
_query_agent: QueryUnderstandingAgent | None = None
_deepener: IterativeDeepener | None = None
_generator: ResponseGenerator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _orchestrator, _query_agent, _deepener, _generator

    api_key = os.environ["ANTHROPIC_API_KEY"]

    os_client = await make_opensearch_client(
        host=os.environ.get("OPENSEARCH_HOST", "localhost"),
        port=int(os.environ.get("OPENSEARCH_PORT", "9200")),
    )
    neo4j_driver = await make_neo4j_driver(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", "bugraidpassword"),
    )
    melt_retriever = MELTRetriever(os.environ.get("MELT_DATA_PATH", "data/melt_telemetry.json"))
    melt_retriever.load()

    _orchestrator = RetrievalOrchestrator(OpenSearchRetriever(os_client), Neo4jRetriever(neo4j_driver), melt_retriever)
    _query_agent = QueryUnderstandingAgent(api_key)
    _deepener = IterativeDeepener(_orchestrator, api_key)
    _generator = ResponseGenerator(api_key)

    logger.info("BugRaid ITOps Research Agent is ready")
    yield

    await os_client.close()
    await neo4j_driver.close()


app = FastAPI(
    title="BugRaid ITOps Research Agent",
    description="Deep research for production incidents — ask anything about your environment",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    question: str
    question_id: str = "Q0"


class HealthResponse(BaseModel):
    status: str
    message: str


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", message="BugRaid ITOps Research Agent is running")


@app.post("/query")
async def query(request: QueryRequest) -> StreamingResponse:
    if not all([_orchestrator, _query_agent, _deepener, _generator]):
        raise HTTPException(status_code=503, detail="Agent not initialized")

    start_ms = now_ms()

    async def event_stream() -> AsyncIterator[str]:
        iterations_used = 1
        confidence = 0.0
        hit_map = {"opensearch": False, "neo4j": False, "melt": False}

        try:
            # Fire query understanding and a broad initial retrieval simultaneously.
            # The broad plan uses the raw text with no filters — good enough for round 1.
            # When the real query plan arrives we use it for deepening, not re-retrieval.
            broad_plan = QueryPlan(
                intent=QueryIntent.GENERAL,
                entities=[],
                sources_needed=[DataSource.OPENSEARCH, DataSource.NEO4J, DataSource.MELT],
                raw_query=request.question,
            )
            query_plan, (evidence, hit_map) = await asyncio.gather(  # type: ignore[misc]
                _query_agent.parse(request.question),  # type: ignore[union-attr]
                _orchestrator.retrieve(broad_plan),    # type: ignore[union-attr]
            )
            logger.info("Q[%s] intent=%s entities=%s", request.question_id, query_plan.intent, query_plan.entities)

            # Simple intents (single-source, well-defined) rarely benefit from extra rounds.
            # Cap them at 1 to avoid spending ~1s per iteration for no gain.
            _SIMPLE_INTENTS = {QueryIntent.DEPLOYMENT_HISTORY, QueryIntent.DEPENDENCY_ANALYSIS, QueryIntent.SERVICE_HEALTH}
            max_iter = 1 if query_plan.intent in _SIMPLE_INTENTS else _deepener.max_iterations

            evidence, iterations_used, hit_map = await _deepener.run(  # type: ignore[union-attr]
                query_plan, evidence, hit_map, max_iterations_override=max_iter
            )
            logger.info("Q[%s] %d iterations, %d sources", request.question_id, iterations_used, len(evidence.sources))

            async for chunk in _generator.generate_streaming(  # type: ignore[union-attr]
                query=request.question,
                evidence=evidence,
                query_plan=query_plan,
                question_id=request.question_id,
                start_time_ms=start_ms,
            ):
                yield chunk

        except Exception as e:
            logger.exception("Query failed: %s", e)
            import json
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

        finally:
            tracker.record(
                latency_ms=now_ms() - start_ms,
                iterations=iterations_used,
                confidence=confidence,
                cost_usd=0.0,  # updated in the done event
                opensearch_hit=hit_map.get("opensearch", False),
                neo4j_hit=hit_map.get("neo4j", False),
                melt_hit=hit_map.get("melt", False),
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # tell nginx not to buffer SSE
        },
    )


@app.get("/metrics")
async def metrics() -> dict:
    return tracker.snapshot()
