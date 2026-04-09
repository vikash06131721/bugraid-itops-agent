from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import anthropic

from src.models import DataSource, QueryIntent, QueryPlan, TimeWindow

logger = logging.getLogger(__name__)

# Listing known services in the prompt helps Claude map "checkout" → "checkout-svc"
KNOWN_SERVICES = [
    "payment-svc", "checkout-svc", "gateway-svc",
    "auth-svc", "inventory-svc", "notification-svc",
]

SYSTEM_PROMPT = """You are the query understanding layer of BugRaid, an ITOps research agent.

Your job is to parse an engineer's natural language question into a structured JSON plan
that tells downstream systems what to look for and where.

The production environment has these services:
  payment-svc, checkout-svc, gateway-svc, auth-svc, inventory-svc, notification-svc

Available data sources:
  opensearch — 500 incident documents with RCA summaries, root causes, resolutions
  neo4j      — Service dependency graph, deployment history, incident graph
  melt       — 7-day telemetry window: metrics (latency, error rate, memory), logs, traces

Intent types (pick the best fit):
  incident_lookup      — questions about past incidents
  service_health       — questions about current status of a service
  deployment_history   — questions about what was deployed and when
  dependency_analysis  — questions about service dependencies
  pattern_analysis     — questions about recurring behaviors or temporal patterns
  multi_doc_synthesis  — questions requiring comparison across multiple incidents
  gap_identification   — questions about what we DON'T know
  general              — anything else

Time window conventions (relative to 2024-11-14T00:00:00Z as "now"):
  "last 24 hours"  → 2024-11-13T00:00:00Z to 2024-11-14T00:00:00Z
  "last month"     → 2024-10-01T00:00:00Z to 2024-10-31T23:59:59Z
  "last week"      → 2024-11-07T00:00:00Z to 2024-11-14T00:00:00Z
  "today"          → 2024-11-14T00:00:00Z to 2024-11-14T23:59:59Z

Return ONLY valid JSON matching this schema:
{
  "intent": "<intent type>",
  "entities": ["<service-name>", ...],
  "time_window": {
    "start": "<ISO 8601 or null>",
    "end": "<ISO 8601 or null>",
    "description": "<human description>"
  },
  "sources_needed": ["opensearch", "neo4j", "melt"],
  "filters": {},
  "ambiguous": false
}

Rules:
- Always include at least 2 sources unless the question is clearly single-source
- If the query is ambiguous or missing a time window, set ambiguous: true and use defaults
- Never return anything other than the JSON object
"""


class QueryUnderstandingAgent:
    def __init__(self, api_key: str) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)

    async def parse(self, query: str) -> QueryPlan:
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": query}],
            )
            raw_json = response.content[0].text.strip()
            return self._parse_response(raw_json, query)

        except Exception as e:
            logger.warning("Query understanding failed (%s), using fallback plan", e)
            return self._fallback_plan(query)

    def _parse_response(self, raw_json: str, original_query: str) -> QueryPlan:
        try:
            # Claude sometimes wraps output in ```json ... ``` fences
            if "```" in raw_json:
                raw_json = raw_json.split("```")[1]
                if raw_json.startswith("json"):
                    raw_json = raw_json[4:]

            data = json.loads(raw_json)

            tw_data = data.get("time_window")
            time_window = None
            if tw_data:
                time_window = TimeWindow(
                    start=datetime.fromisoformat(tw_data["start"].replace("Z", "+00:00")) if tw_data.get("start") else None,
                    end=datetime.fromisoformat(tw_data["end"].replace("Z", "+00:00")) if tw_data.get("end") else None,
                    description=tw_data.get("description", ""),
                )

            sources = [DataSource(s) for s in data.get("sources_needed", ["opensearch", "neo4j", "melt"])]

            return QueryPlan(
                intent=QueryIntent(data.get("intent", "general")),
                entities=data.get("entities", []),
                time_window=time_window,
                sources_needed=sources,
                filters=data.get("filters", {}),
                raw_query=original_query,
                ambiguous=data.get("ambiguous", False),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse query plan JSON: %s", e)
            return self._fallback_plan(original_query)

    def _fallback_plan(self, query: str) -> QueryPlan:
        # Search all three sources with no filters — slower but never breaks
        return QueryPlan(
            intent=QueryIntent.GENERAL,
            entities=[svc for svc in KNOWN_SERVICES if svc in query],
            time_window=None,
            sources_needed=[DataSource.OPENSEARCH, DataSource.NEO4J, DataSource.MELT],
            filters={},
            raw_query=query,
            ambiguous=True,
        )
