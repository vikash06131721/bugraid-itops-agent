"""
Neo4j retriever — raw Cypher over the service dependency graph.
No ORM: graph queries should be readable, not hidden behind abstractions.
"""

from __future__ import annotations

import logging

from neo4j import AsyncDriver, AsyncGraphDatabase

from src.models import DataSource, QueryIntent, QueryPlan, Source

logger = logging.getLogger(__name__)

TOP_K = 10


class Neo4jRetriever:
    def __init__(self, driver: AsyncDriver) -> None:
        self.driver = driver

    async def query(self, query_plan: QueryPlan, extra_context: str = "") -> list[Source]:
        try:
            intent = query_plan.intent
            entities = query_plan.entities
            time_window = query_plan.time_window

            if intent == QueryIntent.DEPENDENCY_ANALYSIS:
                return await self._get_dependencies(entities)

            elif intent == QueryIntent.DEPLOYMENT_HISTORY:
                return await self._get_deployments(entities, time_window)

            elif intent == QueryIntent.INCIDENT_LOOKUP:
                return await self._get_incidents(entities, time_window, query_plan.filters)

            elif intent == QueryIntent.SERVICE_HEALTH:
                return await self._get_service_health(entities)

            elif intent == QueryIntent.MULTI_DOC_SYNTHESIS:
                return await self._get_recent_incidents_with_rca(entities, time_window)

            elif intent == QueryIntent.PATTERN_ANALYSIS:
                return await self._get_incident_patterns(entities)

            elif intent == QueryIntent.GAP_IDENTIFICATION:
                return await self._get_recent_incidents_with_rca(entities, time_window)

            else:
                return await self._general_search(entities)

        except Exception as e:
            logger.warning("Neo4j query failed: %s", e)
            return []

    async def _get_dependencies(self, entities: list[str]) -> list[Source]:
        service_name = entities[0] if entities else "checkout-svc"
        cypher = """
            MATCH (s:Service {name: $name})-[r:DEPENDS_ON]->(dep:Service)
            RETURN dep.name AS dependency,
                   dep.tier AS tier,
                   dep.team AS team,
                   r.latency_ms AS latency_ms,
                   r.criticality AS criticality
            ORDER BY r.criticality DESC, r.latency_ms DESC
        """
        rows = await self._run(cypher, {"name": service_name})
        if not rows:
            return []

        result_text = f"{service_name} depends on: " + ", ".join(
            f"{r['dependency']} ({r['criticality']}, {r['latency_ms']}ms)" for r in rows
        )
        return [Source(
            type=DataSource.NEO4J,
            document_id=f"neo4j-deps-{service_name}",
            relevance_score=0.95,
            excerpt=result_text,
            cypher=cypher,
            metadata={"rows": rows, "service": service_name},
        )]

    async def _get_deployments(self, entities: list[str], time_window) -> list[Source]:
        params: dict = {}
        filters = ""

        if entities:
            service_names = [e for e in entities if "-svc" in e]
            if service_names:
                params["services"] = service_names
                filters += " AND s.name IN $services"

        if time_window and time_window.start:
            params["since"] = time_window.start.isoformat()
            filters += " AND d.timestamp >= $since"

        if time_window and time_window.end:
            params["until"] = time_window.end.isoformat()
            filters += " AND d.timestamp <= $until"

        cypher = f"""
            MATCH (s:Service)-[r:DEPLOYED]->(d:Deployment)
            WHERE 1=1 {filters}
            RETURN s.name AS service,
                   d.version AS version,
                   d.author AS author,
                   d.timestamp AS timestamp,
                   d.change_summary AS change_summary,
                   r.environment AS environment,
                   r.rollback_available AS rollback_available
            ORDER BY d.timestamp DESC
            LIMIT {TOP_K}
        """
        rows = await self._run(cypher, params)
        sources: list[Source] = []
        for row in rows:
            sources.append(Source(
                type=DataSource.NEO4J,
                document_id=f"neo4j-deploy-{row['service']}-{row['version']}",
                relevance_score=0.90,
                excerpt=(
                    f"{row['service']} deployed {row['version']} at {row['timestamp']} "
                    f"by {row['author']}. Changes: {row.get('change_summary', 'N/A')}"
                ),
                cypher=cypher,
                metadata=row,
            ))
        return sources

    async def _get_incidents(self, entities: list[str], time_window, filters: dict) -> list[Source]:
        params: dict = {}
        where_clauses = []

        if entities:
            service_names = [e for e in entities if "-svc" in e]
            if service_names:
                params["services"] = service_names
                where_clauses.append("s.name IN $services")

        if time_window and time_window.start:
            params["since"] = time_window.start.isoformat()
            where_clauses.append("i.timestamp >= $since")

        if time_window and time_window.end:
            params["until"] = time_window.end.isoformat()
            where_clauses.append("i.timestamp <= $until")

        if "severity" in filters:
            params["severity"] = filters["severity"]
            where_clauses.append("i.severity = $severity")

        where_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        cypher = f"""
            MATCH (s:Service)-[:HAD_INCIDENT]->(i:Incident)
            {where_str}
            OPTIONAL MATCH (i)-[:RESOLVED_BY]->(rp:ResolutionPattern)
            RETURN s.name AS service,
                   i.incident_id AS incident_id,
                   i.severity AS severity,
                   i.timestamp AS timestamp,
                   i.duration AS duration,
                   i.resolved AS resolved,
                   rp.pattern_type AS resolution_pattern,
                   rp.success_rate AS resolution_success_rate
            ORDER BY i.timestamp DESC
            LIMIT {TOP_K}
        """
        rows = await self._run(cypher, params)
        sources: list[Source] = []
        for row in rows:
            sources.append(Source(
                type=DataSource.NEO4J,
                document_id=f"neo4j-incident-{row['incident_id']}",
                relevance_score=0.85,
                excerpt=(
                    f"{row['incident_id']}: {row['severity']} on {row['service']} "
                    f"at {row['timestamp']}, lasted {row['duration']}min. "
                    f"Resolution: {row.get('resolution_pattern', 'unknown')}"
                ),
                cypher=cypher,
                metadata=row,
            ))
        return sources

    async def _get_service_health(self, entities: list[str]) -> list[Source]:
        service_name = entities[0] if entities else ""
        cypher = """
            MATCH (s:Service {name: $name})
            OPTIONAL MATCH (s)-[:HAD_INCIDENT]->(i:Incident)
            WHERE i.timestamp >= datetime() - duration('P30D')
            RETURN s.name AS name,
                   s.tier AS tier,
                   s.team AS team,
                   s.sla_minutes AS sla_minutes,
                   count(i) AS recent_incident_count,
                   max(i.timestamp) AS last_incident
            LIMIT 1
        """
        rows = await self._run(cypher, {"name": service_name})
        if not rows:
            return []
        row = rows[0]
        return [Source(
            type=DataSource.NEO4J,
            document_id=f"neo4j-health-{service_name}",
            relevance_score=0.92,
            excerpt=(
                f"{row['name']} is a tier-{row['tier']} service owned by {row['team']}. "
                f"SLA: {row['sla_minutes']}min. Recent incidents (30d): {row['recent_incident_count']}. "
                f"Last incident: {row.get('last_incident', 'none')}"
            ),
            cypher=cypher,
            metadata=row,
        )]

    async def _get_recent_incidents_with_rca(self, entities: list[str], time_window) -> list[Source]:
        params: dict = {}
        service_filter = ""
        if entities:
            service_names = [e for e in entities if "-svc" in e]
            if service_names:
                params["services"] = service_names
                service_filter = "AND s.name IN $services"

        cypher = f"""
            MATCH (s:Service)-[:HAD_INCIDENT]->(i:Incident)-[:RESOLVED_BY]->(rp:ResolutionPattern)
            WHERE i.resolved = true {service_filter}
            RETURN s.name AS service,
                   i.incident_id AS incident_id,
                   i.severity AS severity,
                   i.timestamp AS timestamp,
                   i.duration AS duration,
                   rp.pattern_type AS resolution_pattern,
                   rp.steps AS resolution_steps,
                   rp.success_rate AS success_rate
            ORDER BY i.timestamp DESC
            LIMIT {TOP_K}
        """
        rows = await self._run(cypher, params)
        sources: list[Source] = []
        for row in rows:
            sources.append(Source(
                type=DataSource.NEO4J,
                document_id=f"neo4j-rca-{row['incident_id']}",
                relevance_score=0.88,
                excerpt=(
                    f"{row['incident_id']} ({row['severity']}) on {row['service']}: "
                    f"resolved via {row['resolution_pattern']} in {row['duration']}min. "
                    f"Success rate of this pattern: {row['success_rate']}%"
                ),
                cypher=cypher,
                metadata=row,
            ))
        return sources

    async def _get_incident_patterns(self, entities: list[str]) -> list[Source]:
        """Look for recurring incident patterns — useful for 'why does X happen every Friday'."""
        service_filter = ""
        params: dict = {}
        if entities:
            service_names = [e for e in entities if "-svc" in e]
            if service_names:
                params["services"] = service_names
                service_filter = "WHERE s.name IN $services"

        cypher = f"""
            MATCH (s:Service)-[:HAD_INCIDENT]->(i:Incident)-[:RESOLVED_BY]->(rp:ResolutionPattern)
            {service_filter}
            RETURN rp.pattern_type AS pattern,
                   count(i) AS occurrence_count,
                   avg(i.duration) AS avg_duration_min,
                   collect(i.incident_id)[..5] AS example_incidents
            ORDER BY occurrence_count DESC
            LIMIT 5
        """
        rows = await self._run(cypher, params)
        if not rows:
            return []

        excerpt = "Incident patterns found: " + "; ".join(
            f"{r['pattern']} ({r['occurrence_count']} times, avg {r['avg_duration_min']:.0f}min)"
            for r in rows
        )
        return [Source(
            type=DataSource.NEO4J,
            document_id="neo4j-patterns-" + "-".join(entities[:2]),
            relevance_score=0.82,
            excerpt=excerpt,
            cypher=cypher,
            metadata={"patterns": rows},
        )]

    async def _general_search(self, entities: list[str]) -> list[Source]:
        if not entities:
            return []
        cypher = """
            MATCH (s:Service)
            WHERE s.name IN $names
            RETURN s.name AS name, s.tier AS tier, s.team AS team,
                   s.language AS language, s.sla_minutes AS sla_minutes
        """
        rows = await self._run(cypher, {"names": entities})
        sources: list[Source] = []
        for row in rows:
            sources.append(Source(
                type=DataSource.NEO4J,
                document_id=f"neo4j-service-{row['name']}",
                relevance_score=0.75,
                excerpt=f"{row['name']}: tier-{row['tier']}, team {row['team']}, SLA {row['sla_minutes']}min",
                cypher=cypher,
                metadata=row,
            ))
        return sources

    async def _run(self, cypher: str, params: dict) -> list[dict]:
        async with self.driver.session() as session:
            result = await session.run(cypher, params)
            return [dict(record) async for record in result]


async def make_neo4j_driver(uri: str, user: str, password: str) -> AsyncDriver:
    return AsyncGraphDatabase.driver(uri, auth=(user, password))
