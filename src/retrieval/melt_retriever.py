"""
MELT telemetry retriever.

Loads the 7-day telemetry snapshot into memory at startup. In production
this would hit Datadog / Grafana / Tempo, but a JSON file is enough for the
assessment data without adding extra infrastructure.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.models import DataSource, QueryIntent, QueryPlan, Source

logger = logging.getLogger(__name__)

TOP_K = 10


class MELTRetriever:

    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        self._data: dict = {}
        self._loaded = False

    def load(self) -> None:
        if not self.data_path.exists():
            logger.warning("MELT data file not found at %s — retriever will return empty results", self.data_path)
            self._data = {"metrics": [], "logs": [], "traces": []}
            self._loaded = True
            return

        with open(self.data_path) as f:
            self._data = json.load(f)
        logger.info(
            "Loaded MELT telemetry: %d metrics, %d logs, %d traces",
            len(self._data.get("metrics", [])),
            len(self._data.get("logs", [])),
            len(self._data.get("traces", [])),
        )
        self._loaded = True

    async def fetch(self, query_plan: QueryPlan, extra_context: str = "") -> list[Source]:
        if not self._loaded:
            self.load()

        services = [e for e in query_plan.entities if "-svc" in e]
        intent = query_plan.intent

        if intent == QueryIntent.SERVICE_HEALTH:
            return self._health_snapshot(services)
        elif intent == QueryIntent.PATTERN_ANALYSIS:
            return self._pattern_signals(services, query_plan)
        elif intent in (QueryIntent.INCIDENT_LOOKUP, QueryIntent.GAP_IDENTIFICATION):
            return self._incident_signals(services, query_plan)

        else:
            return self._health_snapshot(services)

    def _health_snapshot(self, services: list[str]) -> list[Source]:
        sources: list[Source] = []

        metrics = self._filter_metrics(services)
        latest_per_name: dict[str, dict] = {}
        for m in sorted(metrics, key=lambda x: x["timestamp"], reverse=True):
            key = f"{m['service']}:{m['name']}"
            if key not in latest_per_name:
                latest_per_name[key] = m

        for key, m in list(latest_per_name.items())[:TOP_K]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-metric-{m['service']}-{m['name'].replace('.', '-')}",
                relevance_score=0.80,
                excerpt=f"{m['service']} {m['name']}: {m['value']} {m['unit']} at {m['timestamp']}",
                metadata=m,
            ))

        logs = self._filter_logs(services, levels=["ERROR", "WARN", "CRITICAL"])
        for log in logs[:5]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-log-{log['service']}-{log['timestamp'].replace(':', '-').replace('.', '-')}",
                relevance_score=0.85,
                excerpt=f"[{log['level']}] {log['service']} @ {log['timestamp']}: {log['message']}",
                metadata=log,
            ))

        return sources[:TOP_K]

    def _pattern_signals(self, services: list[str], query_plan: QueryPlan) -> list[Source]:
        sources: list[Source] = []

        keywords = ["batch", "cron", "scheduled", "weekly", "daily", "reconciliation"]
        all_logs = self._data.get("logs", [])
        pattern_logs = [
            log for log in all_logs
            if any(kw in log.get("message", "").lower() for kw in keywords)
            and (not services or log.get("service") in services or True)  # include all for pattern search
        ]

        for log in sorted(pattern_logs, key=lambda x: x["timestamp"])[:TOP_K]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-pattern-log-{log['service']}-{log['timestamp'].replace(':', '-').replace('.', '-')}",
                relevance_score=0.88,
                excerpt=f"[{log['level']}] {log['service']} @ {log['timestamp']}: {log['message']}",
                metadata=log,
            ))

        metrics = self._filter_metrics(services or [])
        high_latency = [
            m for m in metrics
            if "latency" in m.get("name", "") and m.get("value", 0) > 200  # >200ms is notable
        ]
        for m in sorted(high_latency, key=lambda x: x["value"], reverse=True)[:5]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-latency-{m['service']}-{m['timestamp'].replace(':', '-').replace('.', '-')}",
                relevance_score=0.84,
                excerpt=f"{m['service']} latency spike: {m['value']} {m['unit']} at {m['timestamp']}",
                metadata=m,
            ))

        return sources[:TOP_K]

    def _incident_signals(self, services: list[str], query_plan: QueryPlan) -> list[Source]:
        sources: list[Source] = []

        error_logs = self._filter_logs(services, levels=["ERROR", "CRITICAL"])
        for log in error_logs[:6]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-err-{log['service']}-{log['timestamp'].replace(':', '-').replace('.', '-')}",
                relevance_score=0.90,
                excerpt=f"[{log['level']}] {log['service']} @ {log['timestamp']}: {log['message']}",
                metadata=log,
            ))

        all_traces = self._data.get("traces", [])
        bad_traces = [
            t for t in all_traces
            if (t.get("status") == "error" or t.get("duration_ms", 0) > 1000)
            and (not services or t.get("service") in services)
        ]
        for trace in sorted(bad_traces, key=lambda x: x.get("duration_ms", 0), reverse=True)[:4]:
            sources.append(Source(
                type=DataSource.MELT,
                document_id=f"melt-trace-{trace['trace_id']}",
                relevance_score=0.87,
                excerpt=(
                    f"Trace {trace['trace_id']}: {trace['service']} {trace['operation']} "
                    f"took {trace['duration_ms']}ms, status={trace['status']} at {trace['timestamp']}"
                ),
                metadata=trace,
            ))

        return sources[:TOP_K]

    def _filter_metrics(self, services: list[str]) -> list[dict]:
        all_metrics = self._data.get("metrics", [])
        if not services:
            return all_metrics
        return [m for m in all_metrics if m.get("service") in services]

    def _filter_logs(self, services: list[str], levels: list[str] | None = None) -> list[dict]:
        all_logs = self._data.get("logs", [])
        filtered = all_logs
        if services:
            filtered = [l for l in filtered if l.get("service") in services]
        if levels:
            filtered = [l for l in filtered if l.get("level") in levels]
        return sorted(filtered, key=lambda x: x["timestamp"], reverse=True)
