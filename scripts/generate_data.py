"""
Synthetic data generator for the BugRaid ITOps Research Agent.

Run this to create all four data files in data/:
  python scripts/generate_data.py

Uses a fixed random seed so the data is deterministic — the same run
will always produce the same incidents, deployments, and telemetry.

What gets generated:
  synthetic_incidents.jsonl  — 500 incidents across 6 services over 60 days
  neo4j_seed.cypher          — Cypher to seed 200 service nodes + relationships
  melt_telemetry.json        — 7-day telemetry with a hidden cascade anomaly
  expected_outputs.json      — Ground-truth answers for the 10 test questions
  rca_schema.json            — JSON schema for validating agent output

The hidden cascade (baked into the data at specific timestamps):
  payment-svc memory leak (builds Nov 10-12) →
  checkout-svc latency spike (Nov 12 14:10) →
  gateway-svc retry storm (Nov 12 14:20)

The Friday pattern (for Q8):
  inventory-svc weekly_batch_reconciliation runs every Friday at 18:00 UTC
  It causes checkout-svc p99 latency to spike from ~55ms to ~280ms
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(42)  # deterministic

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICES = ["payment-svc", "checkout-svc", "gateway-svc", "auth-svc", "inventory-svc", "notification-svc"]

# 60-day incident window
WINDOW_START = datetime(2024, 9, 15, 0, 0, 0, tzinfo=timezone.utc)
WINDOW_END   = datetime(2024, 11, 13, 23, 59, 59, tzinfo=timezone.utc)

# 7-day MELT window
MELT_START = datetime(2024, 11, 7, 0, 0, 0, tzinfo=timezone.utc)
MELT_END   = datetime(2024, 11, 13, 23, 59, 59, tzinfo=timezone.utc)

# "Now" for evaluation anchoring
NOW = datetime(2024, 11, 14, 0, 0, 0, tzinfo=timezone.utc)

# The cascade anomaly window
CASCADE_START = datetime(2024, 11, 12, 14, 0, 0, tzinfo=timezone.utc)
CASCADE_END   = datetime(2024, 11, 12, 15, 30, 0, tzinfo=timezone.utc)

# Friday batch job window
FRI_BATCH_START = datetime(2024, 11, 8, 18, 0, 0, tzinfo=timezone.utc)   # Nov 8 is a Friday
FRI_BATCH_END   = datetime(2024, 11, 8, 20, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. synthetic_incidents.jsonl
# ---------------------------------------------------------------------------

ROOT_CAUSES = {
    "payment-svc":    ["connection_pool_exhaustion", "memory_leak", "db_deadlock", "third_party_timeout", "cert_expiry"],
    "checkout-svc":   ["upstream_dependency_latency", "cache_eviction", "db_slow_query", "inventory_lock_contention"],
    "gateway-svc":    ["retry_storm", "rate_limit_exceeded", "circuit_breaker_open", "config_drift", "ssl_handshake_failure"],
    "auth-svc":       ["token_expiry_misconfiguration", "jwks_endpoint_timeout", "session_store_full", "ldap_timeout"],
    "inventory-svc":  ["batch_job_lock", "warehouse_api_timeout", "index_corruption", "replication_lag"],
    "notification-svc": ["smtp_rate_limit", "queue_overflow", "template_render_error", "dead_letter_buildup"],
}

RESOLUTIONS = {
    "connection_pool_exhaustion":     "Restarted pods. Applied connection pool limit patch.",
    "memory_leak":                    "Rolling restart. Added heap monitoring alert. Scheduled fix in next sprint.",
    "db_deadlock":                    "Identified and killed blocking query. Added query timeout.",
    "third_party_timeout":            "Circuit breaker triggered. Increased timeout threshold.",
    "cert_expiry":                    "Renewed certificate. Added 30-day expiry alert.",
    "upstream_dependency_latency":    "Identified upstream degradation in payment-svc. Applied retry with backoff.",
    "cache_eviction":                 "Increased Redis memory. Tuned eviction policy.",
    "db_slow_query":                  "Added index on user_id + created_at. Query time dropped from 4s to 40ms.",
    "inventory_lock_contention":      "Batch job rescheduled to off-peak hours.",
    "retry_storm":                    "Deployed exponential backoff config. Reduced retry rate from 0.87 to 0.02.",
    "rate_limit_exceeded":            "Increased rate limit quota for internal services.",
    "circuit_breaker_open":           "Manual circuit breaker reset after upstream recovery.",
    "config_drift":                   "Deployed corrected config via GitOps pipeline.",
    "ssl_handshake_failure":          "Rotated TLS certificate. Restarted affected pods.",
    "token_expiry_misconfiguration":  "Fixed JWT expiry config. Tokens now expire in 24h.",
    "jwks_endpoint_timeout":          "Increased JWKS endpoint timeout. Added local key cache.",
    "session_store_full":             "Increased Redis session TTL. Evicted stale sessions.",
    "ldap_timeout":                   "Added LDAP connection pooling. Increased timeout.",
    "batch_job_lock":                 "Killed stuck batch job process. Added lock timeout.",
    "warehouse_api_timeout":          "Increased timeout for warehouse API. Added fallback cache.",
    "index_corruption":               "Rebuilt corrupted index from last checkpoint.",
    "replication_lag":                "Increased replication buffer. Promoted new primary.",
    "smtp_rate_limit":                "Switched to secondary SMTP relay. Rate limit increased.",
    "queue_overflow":                 "Scaled consumers. Drained dead letter queue.",
    "template_render_error":          "Fixed broken template variable. Hot-deployed fix.",
    "dead_letter_buildup":            "Replayed dead letter messages. Fixed consumer error.",
    "ssl_handshake_failure":          "Rotated TLS certificates. Restarted load balancers.",
}

TEAMS = {
    "payment-svc":    "payments",
    "checkout-svc":   "commerce",
    "gateway-svc":    "platform",
    "auth-svc":       "identity",
    "inventory-svc":  "fulfillment",
    "notification-svc": "comms",
}


def random_ts(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def generate_incidents() -> list[dict]:
    incidents: list[dict] = []

    # Distribution: 100, 90, 110, 80, 70, 50 = 500 total
    service_counts = {
        "payment-svc": 100, "checkout-svc": 90, "gateway-svc": 110,
        "auth-svc": 80, "inventory-svc": 70, "notification-svc": 50,
    }

    idx = 1
    for svc, count in service_counts.items():
        causes = ROOT_CAUSES[svc]
        for i in range(count):
            ts = random_ts(WINDOW_START, WINDOW_END)

            # Severity distribution per service
            sev_roll = random.random()
            if sev_roll < 0.02:
                severity = "P1"
            elif sev_roll < 0.14:
                severity = "P2"
            elif sev_roll < 0.50:
                severity = "P3"
            else:
                severity = "P4"

            has_rca = severity in ("P1", "P2") or (severity == "P3" and random.random() < 0.43)

            root_cause = random.choice(causes) if has_rca else ""
            resolution = RESOLUTIONS.get(root_cause, "") if has_rca else ""
            affected = [svc]
            if severity in ("P1", "P2") and random.random() < 0.6:
                others = [s for s in SERVICES if s != svc]
                affected += random.sample(others, k=random.randint(1, 2))

            incident = {
                "incident_id": f"INC-2024-{idx:04d}",
                "title": _make_title(svc, root_cause, severity),
                "severity": severity,
                "service": svc,
                "timestamp": ts.isoformat(),
                "duration_minutes": random.randint(5, 240) if severity != "P4" else random.randint(1, 30),
                "affected_services": affected,
                "rca_summary": _make_rca_summary(svc, root_cause) if has_rca else "",
                "root_cause": root_cause,
                "resolution": resolution,
                "tags": _make_tags(svc, root_cause, severity),
                "resolved": True,
            }
            incidents.append(incident)
            idx += 1

    # Inject the canonical cascade incident as a specific P1
    # (replaces a random payment-svc incident)
    cascade = {
        "incident_id": "INC-2024-0487",
        "title": "Payment service latency spike — checkout and gateway cascade",
        "severity": "P1",
        "service": "payment-svc",
        "timestamp": CASCADE_START.isoformat(),
        "duration_minutes": 90,
        "affected_services": ["payment-svc", "checkout-svc", "gateway-svc"],
        "rca_summary": (
            "Memory leak in payment-svc connection pool caused gradual exhaustion over 48 hours. "
            "At 14:00 UTC Nov 12, connection pool hit capacity, causing checkout-svc upstream "
            "timeouts. checkout-svc timeout errors triggered gateway-svc retry storm (retry rate "
            "0.87). Resolved by pod restart at 15:30 UTC and connection pool limit patch."
        ),
        "root_cause": "connection_pool_exhaustion",
        "resolution": (
            "Restarted payment-svc pods at 15:30 UTC. Applied connection pool max-size patch. "
            "Added memory usage alert at 70% threshold. Scheduled heap dump analysis."
        ),
        "tags": ["memory", "latency", "cascade", "P1", "connection-pool", "retry-storm"],
        "resolved": True,
    }
    incidents.append(cascade)

    # Inject a Friday latency incident for Q8 context
    fri_incident = {
        "incident_id": "INC-2024-0312",
        "title": "checkout-svc elevated latency every Friday evening",
        "severity": "P3",
        "service": "checkout-svc",
        "timestamp": datetime(2024, 11, 1, 18, 5, 0, tzinfo=timezone.utc).isoformat(),
        "duration_minutes": 120,
        "affected_services": ["checkout-svc"],
        "rca_summary": (
            "inventory-svc weekly_batch_reconciliation job runs every Friday at 18:00 UTC. "
            "The job performs heavy DB reads on shared tables, causing checkout-svc queries "
            "to queue. p99 latency spikes from ~55ms to ~280ms for the duration of the batch. "
            "Pattern confirmed across 6 consecutive Fridays in incident history."
        ),
        "root_cause": "batch_job_lock",
        "resolution": "Documented as known behavior. Scheduled batch job rescheduling to 02:00 UTC.",
        "tags": ["latency", "batch", "friday", "inventory", "recurring", "P3"],
        "resolved": True,
    }
    incidents.append(fri_incident)

    # Sort by timestamp
    incidents.sort(key=lambda x: x["timestamp"])
    return incidents


def _make_title(svc: str, root_cause: str, severity: str) -> str:
    cause_titles = {
        "connection_pool_exhaustion": f"{svc} connection pool exhaustion",
        "memory_leak": f"{svc} memory leak causing degradation",
        "db_deadlock": f"{svc} database deadlock",
        "retry_storm": f"{svc} retry storm cascading",
        "upstream_dependency_latency": f"{svc} upstream latency degradation",
        "cert_expiry": f"{svc} certificate expiry",
        "batch_job_lock": f"{svc} batch job contention",
        "token_expiry_misconfiguration": f"{svc} token configuration error",
    }
    base = cause_titles.get(root_cause, f"{svc} service degradation")
    return f"[{severity}] {base}" if root_cause else f"[{severity}] {svc} minor incident"


def _make_rca_summary(svc: str, root_cause: str) -> str:
    templates = {
        "connection_pool_exhaustion": f"{svc} hit connection pool limit due to gradual connection leak. Requests began queueing, causing timeout cascade.",
        "memory_leak": f"Memory consumption in {svc} grew steadily over 12-24 hours, leading to OOM and pod restart.",
        "db_deadlock": f"Two concurrent transactions in {svc} acquired locks in opposite order, resulting in deadlock.",
        "retry_storm": f"Upstream failure caused {svc} to retry aggressively. Retry rate reached 0.8+, amplifying the original failure.",
        "upstream_dependency_latency": f"Dependency service degradation caused {svc} requests to time out, increasing error rate.",
        "batch_job_lock": f"Scheduled batch job in {svc} held long-running table locks, blocking real-time request processing.",
    }
    return templates.get(root_cause, f"{svc} experienced {root_cause.replace('_', ' ')}, causing service degradation.")


def _make_tags(svc: str, root_cause: str, severity: str) -> list[str]:
    tags = [severity, TEAMS[svc]]
    cause_tags = {
        "connection_pool_exhaustion": ["connection-pool", "db"],
        "memory_leak": ["memory", "oom"],
        "db_deadlock": ["db", "deadlock"],
        "retry_storm": ["retry", "cascade"],
        "upstream_dependency_latency": ["latency", "dependency"],
        "batch_job_lock": ["batch", "lock", "latency"],
    }
    tags += cause_tags.get(root_cause, [root_cause.replace("_", "-")])
    return tags


# ---------------------------------------------------------------------------
# 2. neo4j_seed.cypher
# ---------------------------------------------------------------------------

# 30 named services (the 6 core + 24 supporting)
NAMED_SERVICES = SERVICES + [
    "user-svc", "order-svc", "catalog-svc", "pricing-svc", "shipping-svc",
    "email-svc", "sms-svc", "fraud-detection-svc", "recommendation-svc", "search-svc",
    "analytics-svc", "reporting-svc", "config-svc", "feature-flag-svc", "audit-svc",
    "file-storage-svc", "media-svc", "cdn-svc", "webhook-svc", "event-bus-svc",
    "scheduler-svc", "worker-svc", "cache-svc", "session-svc",
]

TIERS = {
    "payment-svc": 1, "checkout-svc": 1, "gateway-svc": 1, "auth-svc": 1,
    "inventory-svc": 2, "notification-svc": 3,
    "user-svc": 1, "order-svc": 1, "catalog-svc": 2, "pricing-svc": 2,
    "shipping-svc": 2, "email-svc": 3, "sms-svc": 3, "fraud-detection-svc": 2,
    "recommendation-svc": 3, "search-svc": 2, "analytics-svc": 3, "reporting-svc": 3,
    "config-svc": 1, "feature-flag-svc": 2, "audit-svc": 3, "file-storage-svc": 2,
    "media-svc": 3, "cdn-svc": 2, "webhook-svc": 3, "event-bus-svc": 2,
    "scheduler-svc": 2, "worker-svc": 3, "cache-svc": 1, "session-svc": 1,
}

LANGUAGES = ["Python", "Go", "Java", "Node.js", "Ruby", "Rust"]
TEAMS_FULL = {
    "payment-svc": "payments", "checkout-svc": "commerce", "gateway-svc": "platform",
    "auth-svc": "identity", "inventory-svc": "fulfillment", "notification-svc": "comms",
    "user-svc": "identity", "order-svc": "commerce", "catalog-svc": "catalog",
    "pricing-svc": "commerce", "shipping-svc": "fulfillment", "email-svc": "comms",
    "sms-svc": "comms", "fraud-detection-svc": "payments", "recommendation-svc": "growth",
    "search-svc": "catalog", "analytics-svc": "data", "reporting-svc": "data",
    "config-svc": "platform", "feature-flag-svc": "platform", "audit-svc": "security",
    "file-storage-svc": "platform", "media-svc": "platform", "cdn-svc": "platform",
    "webhook-svc": "platform", "event-bus-svc": "platform", "scheduler-svc": "platform",
    "worker-svc": "platform", "cache-svc": "platform", "session-svc": "identity",
}

# Core dependency graph — the relationships that matter for the test questions
CORE_DEPS = [
    # checkout-svc depends on (Q3)
    ("checkout-svc", "payment-svc",         45,  "critical"),
    ("checkout-svc", "auth-svc",            20,  "critical"),
    ("checkout-svc", "inventory-svc",       35,  "high"),
    ("checkout-svc", "catalog-svc",         15,  "medium"),
    ("checkout-svc", "pricing-svc",         10,  "medium"),
    # payment-svc depends on
    ("payment-svc",  "auth-svc",            15,  "critical"),
    ("payment-svc",  "fraud-detection-svc", 80,  "high"),
    ("payment-svc",  "session-svc",         8,   "medium"),
    # gateway-svc depends on (it's the entry point)
    ("gateway-svc",  "auth-svc",            15,  "critical"),
    ("gateway-svc",  "checkout-svc",        5,   "critical"),
    ("gateway-svc",  "payment-svc",         50,  "high"),
    ("gateway-svc",  "catalog-svc",         12,  "medium"),
    ("gateway-svc",  "search-svc",          20,  "medium"),
    ("gateway-svc",  "feature-flag-svc",    5,   "low"),
    # auth-svc depends on
    ("auth-svc",     "user-svc",            10,  "high"),
    ("auth-svc",     "session-svc",         5,   "critical"),
    ("auth-svc",     "config-svc",          5,   "medium"),
    ("auth-svc",     "audit-svc",           8,   "medium"),
    # inventory-svc depends on
    ("inventory-svc","order-svc",           25,  "high"),
    ("inventory-svc","cache-svc",           5,   "medium"),
    ("inventory-svc","shipping-svc",        30,  "medium"),
    # notification-svc depends on
    ("notification-svc","email-svc",        50,  "high"),
    ("notification-svc","sms-svc",          80,  "medium"),
    ("notification-svc","event-bus-svc",    10,  "high"),
    ("notification-svc","user-svc",         10,  "medium"),
]


def generate_neo4j_cypher(incidents: list[dict]) -> str:
    lines: list[str] = [
        "// BugRaid Neo4j seed script",
        "// Run: cat data/neo4j_seed.cypher | cypher-shell -u neo4j -p bugraidpassword",
        "// Or use the seed_stores.py script which handles this automatically.",
        "",
        "// ─── Clear existing data ───────────────────────────────────────────────────",
        "MATCH (n) DETACH DELETE n;",
        "",
        "// ─── Service nodes (200 total) ─────────────────────────────────────────────",
    ]

    # 30 named services
    all_service_names = list(NAMED_SERVICES)
    # Fill to 200 with generated names
    for i in range(1, 171):
        all_service_names.append(f"internal-svc-{i:03d}")

    for svc in all_service_names:
        tier = TIERS.get(svc, random.randint(2, 3))
        team = TEAMS_FULL.get(svc, f"team-{random.randint(1, 10):02d}")
        lang = LANGUAGES[hash(svc) % len(LANGUAGES)]
        sla = {1: 1440, 2: 4320, 3: 10080}.get(tier, 4320)  # tier-1=24h SLA in minutes... actually let's use:
        sla = {1: 99, 2: 95, 3: 90}.get(tier, 95)  # SLA as percentage uptime target
        lines.append(
            f'CREATE (:Service {{name: "{svc}", tier: {tier}, team: "{team}", '
            f'language: "{lang}", sla_minutes: {sla}}});'
        )

    lines += [
        "",
        "// ─── DEPENDS_ON relationships ─────────────────────────────────────────────",
    ]

    dep_count = 0
    # Core deps first
    for (src, dst, latency, criticality) in CORE_DEPS:
        lines.append(
            f'MATCH (a:Service {{name: "{src}"}}), (b:Service {{name: "{dst}"}}) '
            f'CREATE (a)-[:DEPENDS_ON {{latency_ms: {latency}, criticality: "{criticality}"}}]->(b);'
        )
        dep_count += 1

    # Fill remaining deps between generated services to reach 600
    random.seed(42)
    generated = [s for s in all_service_names if s.startswith("internal-svc-")]
    named_non_core = [s for s in NAMED_SERVICES if s not in [d[0] for d in CORE_DEPS] + [d[1] for d in CORE_DEPS]]

    while dep_count < 600:
        src = random.choice(generated + named_non_core)
        dst = random.choice(generated + named_non_core)
        if src != dst:
            latency = random.randint(5, 200)
            crit = random.choice(["low", "low", "medium", "medium", "high"])
            lines.append(
                f'MATCH (a:Service {{name: "{src}"}}), (b:Service {{name: "{dst}"}}) '
                f'MERGE (a)-[:DEPENDS_ON {{latency_ms: {latency}, criticality: "{crit}"}}]->(b);'
            )
            dep_count += 1

    lines += [
        "",
        "// ─── Deployment nodes (420) ───────────────────────────────────────────────",
    ]

    deploy_count = 0
    # Key deployments for Q2 (last 24h before Nov 14) and Q6 (before major incidents)
    key_deployments = [
        # Q2: deployments on Nov 13 (last 24h)
        ("payment-svc",  "v2.4.2", "2024-11-13T09:15:00Z", "alice.chen",   "Fix connection pool exhaustion. Add memory monitoring.", "production", True),
        ("checkout-svc", "v1.9.3", "2024-11-13T11:30:00Z", "bob.kumar",    "Update payment-svc timeout to 10s. Add retry backoff.",  "production", True),
        ("gateway-svc",  "v4.1.1", "2024-11-13T14:45:00Z", "carla.santos", "Rate limit tuning post-incident. Update retry config.",   "production", True),
        # Q6: deployments before the last 3 major incidents
        # Before cascade (INC-2024-0487 at Nov 12 14:00):
        ("checkout-svc", "v1.9.2", "2024-11-12T10:30:00Z", "bob.kumar",    "Refactor checkout flow. Remove deprecated payment endpoint.", "production", True),
        # Before gateway P1 on Oct 28:
        ("gateway-svc",  "v4.0.9", "2024-10-28T11:00:00Z", "carla.santos", "Increase retry count from 3 to 5 for downstream services.", "production", True),
        # Before auth P1 on Oct 14:
        ("auth-svc",     "v3.0.4", "2024-10-14T09:00:00Z", "diana.reyes",  "JWT expiry change from 1h to 8h. Update JWKS endpoint URL.", "production", False),
    ]

    for (svc, ver, ts, author, summary, env, rollback) in key_deployments:
        deploy_id = f"deploy-{svc}-{ver}".replace(".", "-")
        lines.append(
            f'MATCH (s:Service {{name: "{svc}"}}) '
            f'CREATE (d:Deployment {{version: "{ver}", author: "{author}", timestamp: "{ts}", '
            f'change_summary: "{summary}"}}) '
            f'CREATE (s)-[:DEPLOYED {{environment: "{env}", rollback_available: {str(rollback).lower()}}}]->(d);'
        )
        deploy_count += 1

    # Fill remaining deployments across all services to reach 420
    authors = ["alice.chen", "bob.kumar", "carla.santos", "diana.reyes", "evan.park", "fiona.walsh", "george.li"]
    for svc in all_service_names:
        while deploy_count < 420:
            n_deploys = random.randint(1, 4)
            for _ in range(n_deploys):
                if deploy_count >= 420:
                    break
                ts = random_ts(WINDOW_START, WINDOW_END - timedelta(days=2))
                major = random.randint(1, 5)
                minor = random.randint(0, 20)
                patch = random.randint(0, 10)
                ver = f"v{major}.{minor}.{patch}"
                author = random.choice(authors)
                rollback = random.choice([True, False])
                lines.append(
                    f'MATCH (s:Service {{name: "{svc}"}}) '
                    f'CREATE (d:Deployment {{version: "{ver}", author: "{author}", timestamp: "{ts.isoformat()}", '
                    f'change_summary: "Routine update"}}) '
                    f'CREATE (s)-[:DEPLOYED {{environment: "production", rollback_available: {str(rollback).lower()}}}]->(d);'
                )
                deploy_count += 1
            if deploy_count >= 420:
                break

    lines += [
        "",
        "// ─── Incident nodes (150 — the ones with full RCA) ───────────────────────",
    ]

    # Pick incidents that have full RCA
    rca_incidents = [inc for inc in incidents if inc.get("rca_summary") and inc.get("root_cause")][:150]
    for inc in rca_incidents:
        svc = inc["service"]
        iid = inc["incident_id"]
        sev = inc["severity"]
        ts = inc["timestamp"]
        dur = inc["duration_minutes"]
        resolved = str(inc["resolved"]).lower()
        lines.append(
            f'MATCH (s:Service {{name: "{svc}"}}) '
            f'CREATE (i:Incident {{incident_id: "{iid}", severity: "{sev}", timestamp: "{ts}", '
            f'duration: {dur}, resolved: {resolved}}}) '
            f'CREATE (s)-[:HAD_INCIDENT {{impact_score: {random.uniform(0.3, 1.0):.2f}}}]->(i);'
        )

    lines += [
        "",
        "// ─── ResolutionPattern nodes (40) ────────────────────────────────────────",
    ]

    resolution_patterns = [
        ("pod_restart",               "Pod restart with memory flush",    95, '["1. Identify OOM pod", "2. kubectl rollout restart", "3. Monitor memory"]'),
        ("connection_pool_patch",     "Connection pool limit adjustment",  88, '["1. Check pool metrics", "2. Adjust max_pool_size", "3. Deploy config"]'),
        ("circuit_breaker_reset",     "Manual circuit breaker reset",      92, '["1. Confirm upstream healthy", "2. Reset circuit breaker", "3. Monitor error rate"]'),
        ("retry_backoff_deploy",      "Exponential backoff deployment",    85, '["1. Deploy retry config", "2. Verify timing logs", "3. Monitor retry rate"]'),
        ("index_rebuild",             "Database index rebuild",            78, '["1. Identify slow query", "2. Add index", "3. Verify query plan"]'),
        ("cert_rotation",             "Certificate rotation",              99, '["1. Generate new cert", "2. Deploy to all instances", "3. Verify TLS"]'),
        ("config_rollback",           "Configuration rollback via GitOps", 90, '["1. Revert config PR", "2. Apply via ArgoCD", "3. Verify deployment"]'),
        ("batch_job_reschedule",      "Batch job rescheduled to off-peak", 82, '["1. Identify batch window", "2. Update cron schedule", "3. Test off-peak run"]'),
        ("rate_limit_increase",       "Rate limit quota increase",         88, '["1. Identify limit breach", "2. Submit quota increase", "3. Monitor throughput"]'),
        ("cache_invalidation",        "Cache invalidation and warmup",     75, '["1. Identify stale keys", "2. Flush affected keys", "3. Warm up cache"]'),
    ]

    # Add more to reach 40
    extra_patterns = [
        (f"pattern-{i:02d}", f"Resolution pattern type {i}", random.randint(60, 95),
         '["Step 1", "Step 2", "Step 3"]')
        for i in range(11, 41)
    ]

    all_patterns = [(p[0], p[1], p[2], p[3]) for p in resolution_patterns] + extra_patterns

    for (ptype, name, success_rate, steps) in all_patterns[:40]:
        lines.append(
            f'CREATE (:ResolutionPattern {{pattern_type: "{ptype}", name: "{name}", '
            f'success_rate: {success_rate}, steps: {steps}}});'
        )

    # Link some incidents to resolution patterns
    lines += [
        "",
        "// ─── RESOLVED_BY relationships (40) ─────────────────────────────────────",
    ]

    for inc in rca_incidents[:40]:
        iid = inc["incident_id"]
        root_cause = inc.get("root_cause", "")
        cause_to_pattern = {
            "connection_pool_exhaustion": "pod_restart",
            "memory_leak": "pod_restart",
            "retry_storm": "retry_backoff_deploy",
            "db_deadlock": "index_rebuild",
            "cert_expiry": "cert_rotation",
            "config_drift": "config_rollback",
            "batch_job_lock": "batch_job_reschedule",
            "rate_limit_exceeded": "rate_limit_increase",
            "cache_eviction": "cache_invalidation",
        }
        pattern = cause_to_pattern.get(root_cause, "pod_restart")
        lines.append(
            f'MATCH (i:Incident {{incident_id: "{iid}"}}), '
            f'(rp:ResolutionPattern {{pattern_type: "{pattern}"}}) '
            f'CREATE (i)-[:RESOLVED_BY {{confidence_score: {random.uniform(0.7, 1.0):.2f}}}]->(rp);'
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. melt_telemetry.json
# ---------------------------------------------------------------------------

def generate_melt() -> dict:
    metrics: list[dict] = []
    logs: list[dict] = []
    traces: list[dict] = []

    # Generate baseline metrics for all services over 7 days
    # Every 5 minutes = 12 per hour = 288 per day = 2016 per service
    baseline_memory   = {"payment-svc": 55, "checkout-svc": 40, "gateway-svc": 35, "auth-svc": 30, "inventory-svc": 45, "notification-svc": 25}
    baseline_latency  = {"payment-svc": 45, "checkout-svc": 55, "gateway-svc": 12, "auth-svc": 18, "inventory-svc": 60, "notification-svc": 80}
    baseline_err_rate = {"payment-svc": 0.1, "checkout-svc": 0.2, "gateway-svc": 0.15, "auth-svc": 0.1, "inventory-svc": 0.3, "notification-svc": 0.5}
    baseline_rps      = {"payment-svc": 850, "checkout-svc": 1200, "gateway-svc": 5000, "auth-svc": 2000, "inventory-svc": 400, "notification-svc": 200}

    current_time = MELT_START
    trace_id_counter = 1000

    while current_time <= MELT_END:
        ts = current_time.isoformat()
        hour = current_time.hour
        dow = current_time.weekday()  # 4 = Friday

        for svc in SERVICES:
            # Determine if we're in the cascade window
            in_cascade = CASCADE_START <= current_time <= CASCADE_END
            # payment-svc memory leak builds over Nov 10-12
            days_into_leak = max(0, (current_time - datetime(2024, 11, 10, tzinfo=timezone.utc)).total_seconds() / 86400)
            in_leak_buildup = datetime(2024, 11, 10, tzinfo=timezone.utc) <= current_time <= CASCADE_END

            # Determine if we're in the Friday batch window
            in_friday_batch = (dow == 4 and 18 <= hour < 20)

            # Memory metric
            mem_base = baseline_memory[svc]
            if svc == "payment-svc" and in_leak_buildup:
                mem_value = mem_base + min(25, days_into_leak * 6) + random.uniform(-2, 2)
            elif in_cascade and svc in ("checkout-svc", "gateway-svc"):
                mem_value = mem_base * 1.1 + random.uniform(-2, 2)
            else:
                mem_value = mem_base + random.uniform(-3, 3)
            metrics.append({"timestamp": ts, "service": svc, "name": "memory_used_percent", "value": round(mem_value, 1), "unit": "%"})

            # Latency metric (p99)
            lat_base = baseline_latency[svc]
            if in_cascade:
                if svc == "payment-svc":
                    lat_value = lat_base * 8 + random.uniform(-20, 20)  # huge spike
                elif svc == "checkout-svc" and current_time >= CASCADE_START + timedelta(minutes=10):
                    lat_value = lat_base * 40 + random.uniform(-100, 100)  # ~2200ms
                elif svc == "gateway-svc" and current_time >= CASCADE_START + timedelta(minutes=20):
                    lat_value = lat_base * 5 + random.uniform(-10, 10)
                else:
                    lat_value = lat_base + random.uniform(-5, 5)
            elif in_friday_batch and svc == "checkout-svc":
                lat_value = lat_base * 5 + random.uniform(-20, 20)  # ~280ms
            elif in_friday_batch and svc == "inventory-svc":
                lat_value = lat_base * 3 + random.uniform(-10, 10)
            else:
                # Normal business-hours increase
                business_factor = 1.0 + (0.3 if 9 <= hour <= 18 else 0.0)
                lat_value = lat_base * business_factor + random.uniform(-5, 5)
            metrics.append({"timestamp": ts, "service": svc, "name": "p99_latency_ms", "value": round(max(1, lat_value), 1), "unit": "ms"})

            # Error rate
            err_base = baseline_err_rate[svc]
            if in_cascade:
                if svc == "payment-svc":
                    err_value = 0.45
                elif svc == "checkout-svc" and current_time >= CASCADE_START + timedelta(minutes=10):
                    err_value = 0.65
                elif svc == "gateway-svc" and current_time >= CASCADE_START + timedelta(minutes=20):
                    err_value = 0.82
                else:
                    err_value = err_base
            else:
                err_value = err_base + random.uniform(-0.05, 0.05)
            metrics.append({"timestamp": ts, "service": svc, "name": "error_rate", "value": round(max(0, err_value), 3), "unit": "fraction"})

            # RPS
            rps_base = baseline_rps[svc]
            rps = rps_base * (1.0 + 0.4 * (1 if 9 <= hour <= 18 else 0)) + random.uniform(-50, 50)
            metrics.append({"timestamp": ts, "service": svc, "name": "requests_per_second", "value": round(max(0, rps), 1), "unit": "rps"})

            # Connection pool for payment-svc
            if svc == "payment-svc":
                if in_leak_buildup:
                    pool_pct = min(100, 45 + days_into_leak * 15 + random.uniform(-3, 3))
                elif in_cascade:
                    pool_pct = 100.0  # exhausted
                else:
                    pool_pct = 45 + random.uniform(-5, 5)  # recovery
                metrics.append({"timestamp": ts, "service": svc, "name": "connection_pool_utilization", "value": round(pool_pct, 1), "unit": "%"})

            # Retry rate for gateway-svc
            if svc == "gateway-svc":
                if in_cascade and current_time >= CASCADE_START + timedelta(minutes=20):
                    retry_rate = 0.87
                else:
                    retry_rate = 0.02 + random.uniform(-0.005, 0.005)
                metrics.append({"timestamp": ts, "service": svc, "name": "retry_rate", "value": round(retry_rate, 3), "unit": "fraction"})

        current_time += timedelta(minutes=5)

    # Log entries — key events
    key_log_events = [
        # Memory leak warning build-up
        {"timestamp": "2024-11-10T06:00:00Z", "service": "payment-svc", "level": "WARN",     "message": "Connection pool utilization above 60%. Current: 62%. Baseline: 45%.", "trace_id": "t-0001"},
        {"timestamp": "2024-11-11T14:00:00Z", "service": "payment-svc", "level": "WARN",     "message": "Memory usage at 68%. Slow growth trend detected over last 24h.", "trace_id": "t-0002"},
        {"timestamp": "2024-11-12T08:00:00Z", "service": "payment-svc", "level": "WARN",     "message": "Connection pool utilization at 85%. Approaching capacity.", "trace_id": "t-0003"},
        {"timestamp": "2024-11-12T13:30:00Z", "service": "payment-svc", "level": "WARN",     "message": "Memory at 78%. Connection pool at 95%. Investigation recommended.", "trace_id": "t-0004"},
        # The cascade
        {"timestamp": "2024-11-12T14:00:00Z", "service": "payment-svc", "level": "ERROR",    "message": "connection_pool_exhaustion: all 200 connections in use. New requests queuing.", "trace_id": "t-0100"},
        {"timestamp": "2024-11-12T14:02:00Z", "service": "payment-svc", "level": "ERROR",    "message": "Request timeout: connection pool wait exceeded 5000ms threshold.", "trace_id": "t-0101"},
        {"timestamp": "2024-11-12T14:10:00Z", "service": "checkout-svc", "level": "ERROR",   "message": "Upstream timeout from payment-svc. HTTP 504. Retry 1/3 in 2s.", "trace_id": "t-0110"},
        {"timestamp": "2024-11-12T14:11:00Z", "service": "checkout-svc", "level": "ERROR",   "message": "Upstream timeout from payment-svc. All 3 retries exhausted. Failing request.", "trace_id": "t-0111"},
        {"timestamp": "2024-11-12T14:20:00Z", "service": "gateway-svc",  "level": "ERROR",   "message": "Downstream checkout-svc error rate elevated. Retry storm detected. Rate: 0.87.", "trace_id": "t-0120"},
        {"timestamp": "2024-11-12T14:25:00Z", "service": "gateway-svc",  "level": "CRITICAL","message": "Circuit breaker OPEN for checkout-svc. Retry rate exceeded threshold 0.85.", "trace_id": "t-0121"},
        # Recovery
        {"timestamp": "2024-11-12T15:30:00Z", "service": "payment-svc", "level": "INFO",     "message": "Pod restart initiated. Reason: connection_pool_exhaustion. ETA: 90s.", "trace_id": "t-0200"},
        {"timestamp": "2024-11-12T15:32:00Z", "service": "payment-svc", "level": "INFO",     "message": "Pod restarted successfully. Connection pool cleared. Pool utilization: 3%.", "trace_id": "t-0201"},
        {"timestamp": "2024-11-12T15:35:00Z", "service": "checkout-svc", "level": "INFO",    "message": "payment-svc responding normally. Error rate returning to baseline.", "trace_id": "t-0210"},
        {"timestamp": "2024-11-12T15:40:00Z", "service": "gateway-svc",  "level": "INFO",    "message": "Circuit breaker CLOSED for checkout-svc. Retry rate: 0.03.", "trace_id": "t-0220"},
        # Friday batch job (Nov 8)
        {"timestamp": "2024-11-08T18:00:00Z", "service": "inventory-svc","level": "INFO",    "message": "weekly_batch_reconciliation started. Estimated duration: 2h. Table locks acquired.", "trace_id": "t-0300"},
        {"timestamp": "2024-11-08T18:02:00Z", "service": "checkout-svc", "level": "WARN",   "message": "Elevated latency on inventory lookups. DB wait time: 380ms (baseline: 35ms).", "trace_id": "t-0301"},
        {"timestamp": "2024-11-08T18:05:00Z", "service": "checkout-svc", "level": "WARN",   "message": "p99 latency: 287ms. Inventory-svc contention likely. Batch job in progress.", "trace_id": "t-0302"},
        {"timestamp": "2024-11-08T20:00:00Z", "service": "inventory-svc","level": "INFO",    "message": "weekly_batch_reconciliation completed. 847,293 records reconciled. Locks released.", "trace_id": "t-0310"},
        {"timestamp": "2024-11-08T20:02:00Z", "service": "checkout-svc", "level": "INFO",   "message": "Latency returning to baseline. p99: 58ms.", "trace_id": "t-0311"},
        # Normal operational logs
        {"timestamp": "2024-11-07T09:00:00Z", "service": "auth-svc",     "level": "INFO",    "message": "JWKS keys rotated successfully. New key_id: key-2024-11-07.", "trace_id": "t-0400"},
        {"timestamp": "2024-11-09T14:00:00Z", "service": "payment-svc",  "level": "INFO",    "message": "Scheduled maintenance completed. No issues found.", "trace_id": "t-0401"},
        {"timestamp": "2024-11-13T09:20:00Z", "service": "payment-svc",  "level": "INFO",    "message": "v2.4.2 deployed. Connection pool max_size increased to 300. Memory monitoring added.", "trace_id": "t-0402"},
    ]

    logs.extend(key_log_events)

    # Key traces for the cascade
    cascade_traces = [
        {"trace_id": "trace-cascade-001", "service": "gateway-svc",   "operation": "POST /checkout", "duration_ms": 8420, "status": "error", "timestamp": "2024-11-12T14:10:30Z"},
        {"trace_id": "trace-cascade-001", "service": "checkout-svc",  "operation": "process_payment", "duration_ms": 8200, "status": "error", "timestamp": "2024-11-12T14:10:31Z"},
        {"trace_id": "trace-cascade-001", "service": "payment-svc",   "operation": "get_connection",  "duration_ms": 5010, "status": "error", "timestamp": "2024-11-12T14:10:32Z"},
        {"trace_id": "trace-friday-001",  "service": "checkout-svc",  "operation": "check_inventory", "duration_ms": 412,  "status": "slow",  "timestamp": "2024-11-08T18:10:00Z"},
        {"trace_id": "trace-friday-001",  "service": "inventory-svc", "operation": "db_query",        "duration_ms": 380,  "status": "slow",  "timestamp": "2024-11-08T18:10:01Z"},
    ]
    traces.extend(cascade_traces)

    return {
        "window": {
            "start": MELT_START.isoformat(),
            "end": MELT_END.isoformat(),
        },
        "now": NOW.isoformat(),
        "metrics": metrics,
        "logs": logs,
        "traces": traces,
    }


# ---------------------------------------------------------------------------
# 4. expected_outputs.json
# ---------------------------------------------------------------------------

def generate_expected_outputs() -> list[dict]:
    return [
        {
            "question_id": "Q1",
            "question": "What is the payment service responsible for?",
            "key_facts": [
                "payment-svc handles all payment processing",
                "tier-1 service",
                "depends on auth-svc and fraud-detection-svc",
                "has had P1 incidents involving connection pool exhaustion",
            ],
            "primary_source": "opensearch",
            "difficulty": "low",
        },
        {
            "question_id": "Q2",
            "question": "What deployments happened in the last 24 hours?",
            "key_facts": [
                "payment-svc v2.4.2 deployed 2024-11-13T09:15:00Z",
                "checkout-svc v1.9.3 deployed 2024-11-13T11:30:00Z",
                "gateway-svc v4.1.1 deployed 2024-11-13T14:45:00Z",
            ],
            "primary_source": "neo4j",
            "difficulty": "low",
        },
        {
            "question_id": "Q3",
            "question": "Which services does checkout-svc depend on?",
            "key_facts": [
                "payment-svc (critical, 45ms)",
                "auth-svc (critical, 20ms)",
                "inventory-svc (high, 35ms)",
                "catalog-svc (medium, 15ms)",
                "pricing-svc (medium, 10ms)",
            ],
            "primary_source": "neo4j",
            "difficulty": "low",
        },
        {
            "question_id": "Q4",
            "question": "What incidents involved auth-svc last month?",
            "key_facts": [
                "multiple incidents in October 2024",
                "severity range P1-P3",
                "common patterns: token expiry, JWKS timeouts",
            ],
            "primary_source": "opensearch+neo4j",
            "difficulty": "medium",
        },
        {
            "question_id": "Q5",
            "question": "Is payment-svc healthy right now?",
            "key_facts": [
                "mostly healthy after 2024-11-12 incident",
                "memory still slightly elevated post-incident",
                "connection pool recovering",
                "v2.4.2 deployed with pool fix",
            ],
            "primary_source": "melt+neo4j",
            "difficulty": "medium",
        },
        {
            "question_id": "Q6",
            "question": "What changed before the last 3 major incidents?",
            "key_facts": [
                "checkout-svc v1.9.2 deployed 3.5h before INC-2024-0487",
                "gateway-svc v4.0.9 deployed 2h before Oct 28 P1",
                "auth-svc v3.0.4 deployed 4h before Oct 14 P1",
                "deployment-to-incident pattern is consistent",
            ],
            "primary_source": "neo4j+opensearch",
            "difficulty": "hard",
        },
        {
            "question_id": "Q7",
            "question": "Which service is the most fragile in our system?",
            "key_facts": [
                "gateway-svc has highest total incident count",
                "gateway-svc has highest dependency fan-in (35 DEPENDS_ON relationships)",
                "payment-svc has most P1 incidents",
                "gateway-svc amplifies failures via retry storms",
            ],
            "primary_source": "neo4j+melt",
            "difficulty": "hard",
        },
        {
            "question_id": "Q8",
            "question": "Why does checkout slow down every Friday evening?",
            "key_facts": [
                "inventory-svc weekly_batch_reconciliation starts at 18:00 UTC every Friday",
                "batch job acquires table locks on shared DB",
                "checkout-svc p99 latency spikes from ~55ms to ~280ms",
                "duration: approximately 2 hours",
                "pattern confirmed in logs and MELT traces",
            ],
            "primary_source": "melt",
            "difficulty": "hard",
        },
        {
            "question_id": "Q9",
            "question": "Compare how we resolved the last 5 payment incidents",
            "key_facts": [
                "pod restart is most common resolution (3 of 5)",
                "connection pool is recurring root cause",
                "average resolution time ~90 minutes for P1s",
                "patch deployment pattern after restart",
            ],
            "primary_source": "opensearch",
            "difficulty": "very_hard",
        },
        {
            "question_id": "Q10",
            "question": "What don't we know about today's incident?",
            "key_facts": [
                "no heap dump captured — exact memory leak source unknown",
                "distributed traces incomplete — gateway-svc traces missing correlation IDs",
                "customer transaction impact count not available in telemetry",
                "unclear if connection pool fix is permanent or masks deeper memory issue",
                "no post-mortem on whether checkout-svc v1.9.2 deployment contributed",
            ],
            "primary_source": "gap_identification",
            "difficulty": "expert",
        },
    ]


# ---------------------------------------------------------------------------
# 5. rca_schema.json
# ---------------------------------------------------------------------------

def generate_rca_schema() -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "BugRaid Research Response",
        "type": "object",
        "required": ["question_id", "question", "answer", "confidence", "iterations_used", "sources", "claims", "knowledge_gaps"],
        "properties": {
            "question_id": {"type": "string"},
            "question": {"type": "string"},
            "answer": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "iterations_used": {"type": "integer", "minimum": 1, "maximum": 3},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type", "document_id", "relevance_score", "excerpt"],
                    "properties": {
                        "type": {"type": "string", "enum": ["opensearch", "neo4j", "melt"]},
                        "document_id": {"type": "string"},
                        "relevance_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "excerpt": {"type": "string"},
                    },
                },
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["claim", "confidence", "source_id"],
                    "properties": {
                        "claim": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "source_id": {"type": "string"},
                    },
                },
            },
            "knowledge_gaps": {"type": "array", "items": {"type": "string"}},
            "latency_ms": {"type": "integer"},
            "token_usage": {"type": "object"},
            "cost_usd": {"type": "number"},
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Generating synthetic data...")

    # 1. Incidents
    print("  → 500 incidents...")
    incidents = generate_incidents()
    with open(DATA_DIR / "synthetic_incidents.jsonl", "w") as f:
        for inc in incidents:
            f.write(json.dumps(inc) + "\n")
    print(f"     ✓ {len(incidents)} incidents written")

    # 2. Neo4j Cypher
    print("  → Neo4j seed script...")
    cypher = generate_neo4j_cypher(incidents)
    with open(DATA_DIR / "neo4j_seed.cypher", "w") as f:
        f.write(cypher)
    print("     ✓ neo4j_seed.cypher written")

    # 3. MELT telemetry
    print("  → MELT telemetry (7 days × 6 services, 5-min intervals)...")
    melt = generate_melt()
    with open(DATA_DIR / "melt_telemetry.json", "w") as f:
        json.dump(melt, f, indent=2)
    print(f"     ✓ {len(melt['metrics'])} metrics, {len(melt['logs'])} logs, {len(melt['traces'])} traces")

    # 4. Expected outputs
    print("  → Expected outputs for 10 test questions...")
    outputs = generate_expected_outputs()
    with open(DATA_DIR / "expected_outputs.json", "w") as f:
        json.dump(outputs, f, indent=2)
    print("     ✓ expected_outputs.json written")

    # 5. RCA schema
    schema = generate_rca_schema()
    with open(DATA_DIR / "rca_schema.json", "w") as f:
        json.dump(schema, f, indent=2)
    print("     ✓ rca_schema.json written")

    print("\nAll data files written to data/")
    print("Next: python scripts/seed_stores.py")


if __name__ == "__main__":
    main()
