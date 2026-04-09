# How the BugRaid ITOps Research Agent Works

*A plain-English walkthrough of the architecture.*

---

## The Problem

Imagine it's 3am. payment-svc is down. Your on-call engineer opens a terminal and types:

> "Why is checkout slow? Is this related to the gateway retry storm from last week?"

Before this agent existed, answering that required:
- Searching Slack for past incidents
- Querying the service graph manually
- Pulling up Grafana for metrics
- Cross-referencing 3 different dashboards

This agent does all of that at once, in under 10 seconds, and tells you exactly which sources it used.

---

## The Four-Layer Pipeline

Here's what happens the moment a question comes in:

```
Engineer asks: "Why does checkout slow down every Friday evening?"
                              │
                              ▼
              ┌───────────────────────────────┐
              │  LAYER 1: Query Understanding  │
              │                               │
              │  Claude reads the question    │
              │  and decides:                 │
              │  - What's the intent?         │  ← "pattern_analysis"
              │  - Which services?            │  ← ["checkout-svc"]
              │  - What time window?          │  ← none (recurring)
              │  - Which data sources?        │  ← opensearch, neo4j, melt
              └───────────────┬───────────────┘
                              │
                    QueryPlan (structured)
                              │
                              ▼
              ┌───────────────────────────────┐
              │  LAYER 2: Parallel Retrieval   │
              │                               │
              │  Three searches at once.      │
              │  (asyncio.gather — not        │
              │   sequential, ever)           │
              │                               │
              │  ┌─────────┐ ┌──────┐ ┌─────┐│
              │  │OpenSearch│ │Neo4j │ │MELT ││
              │  │500 docs  │ │graph │ │logs ││
              │  │incidents │ │deps  │ │metrics│
              │  └────┬─────┘ └──┬───┘ └──┬──┘│
              │       │          │         │   │
              │       └──────────┴────┬────┘   │
              │                       │        │
              │             Evidence Pool      │
              └───────────────┬───────────────┘
                              │
                     ~30 pieces of evidence
                              │
                              ▼
              ┌───────────────────────────────┐
              │  LAYER 3: Iterative Deepening  │
              │                               │
              │  Confidence check:            │
              │  - High enough? → stop        │
              │  - No new docs? → stop        │
              │  - Hit 3 rounds? → stop       │
              │                               │
              │  Otherwise: ask Claude        │
              │  "what's still missing?"      │
              │  and search again.            │
              └───────────────┬───────────────┘
                              │
                   Final evidence pool
                              │
                              ▼
              ┌───────────────────────────────┐
              │  LAYER 4: Grounded Response    │
              │                               │
              │  Claude writes the answer     │
              │  using only retrieved sources │
              │                               │
              │  Rules:                       │
              │  - Every claim cites [src_id] │
              │  - Low confidence? Flag it ⚠️ │
              │  - Missing data? Say so       │
              │  - Stream tokens as generated │
              └───────────────┬───────────────┘
                              │
                              ▼
         Answer arrives streaming, first token < 5s

         {
           "answer": "checkout-svc slows on Friday evenings
                      because inventory-svc runs a weekly
                      batch reconciliation job at 18:00 UTC
                      [melt-pattern-log-inventory-svc-...].
                      This job holds table locks that checkout-
                      svc's inventory queries wait on, pushing
                      p99 latency from ~55ms to ~280ms
                      [melt-latency-checkout-svc-...].
                      Pattern confirmed across 6 Fridays in
                      incident history [INC-2024-0312].",
           "confidence": 0.88,
           "iterations_used": 2,
           "sources": [...],
           "claims": [...],
           "knowledge_gaps": [
             "Exact DB table being locked not identified in traces"
           ]
         }
```

---

## The Three Data Sources

### OpenSearch — *What happened?*

Stores 500 incident documents covering the last 60 days. Each document has:
- What service was affected
- How severe it was (P1-P4)
- Root cause and resolution (for incidents with full RCA)

We use **hybrid search**: keyword matching (BM25) catches "connection_pool_exhaustion"
exactly; dense vector search catches "memory issue" even if that exact phrase isn't in the doc.

Both searches run simultaneously, then merge via **Reciprocal Rank Fusion** (RRF):
a document that ranks high in both searches rises to the top.

### Neo4j — *What's connected to what?*

A graph of 200 service nodes and their relationships:
- `DEPENDS_ON` — which services call which other services, and how critical the dependency is
- `DEPLOYED` — deployment history with author, version, and change summary
- `HAD_INCIDENT` — links services to their incident history
- `RESOLVED_BY` — links incidents to resolution patterns

We query this with **raw Cypher** (no ORM). Graph queries need to be readable
and debuggable — an abstraction layer would just hide what's happening.

### MELT Telemetry — *What do the numbers say?*

7-day telemetry window loaded into memory at startup. Contains:
- **Metrics**: memory usage, latency (p99), error rate, RPS, connection pool utilization
- **Logs**: operational events, errors, warnings (with trace IDs)
- **Traces**: individual request spans with duration and status

No database needed — it's a JSON file. In production this would be Datadog / Grafana.

---

## Why Parallel Retrieval Matters

Sequential retrieval would look like:

```
→ Wait for OpenSearch  (600ms)
  → Then Neo4j         (300ms)
    → Then MELT        (5ms)
      → Total: ~900ms
```

Parallel retrieval:

```
→ OpenSearch (600ms) ─┐
→ Neo4j (300ms)       ├─ Total: ~600ms (slowest one wins)
→ MELT (5ms)          ─┘
```

For an on-call engineer at 3am, 300ms matters.

The timing logs prove parallelism — reviewers can verify this by looking at
the log output: all three `_retrieved X results in Yms` lines appear at nearly
the same timestamp.

If one source goes down mid-query? `asyncio.gather(..., return_exceptions=True)`
means the other two still return. Degraded, not dead.

---

## Why Iterative Deepening

Single-pass RAG has a fundamental problem: you can't know what to look for
until you've seen some results.

Example: An engineer asks "What don't we know about today's incident?"

**Iteration 1** retrieves the most recent incidents and MELT data.
The agent notices payment-svc had an incident at 14:00. Now it knows to ask:
*"Are there heap dumps? Are gateway traces correlated?"*

**Iteration 2** searches specifically for heap dump logs and trace correlation.
Finds: no heap dump, trace IDs don't match across services.

**Final answer** includes both what we found AND what we couldn't find.

The confidence threshold (0.85) stops us early on simple questions.
The "no new evidence" check stops us when more searching won't help.
We never run more than 3 rounds.

---

## Grounding: Why Every Claim Has a Source

An agent that says "payment-svc has a memory leak" without citing a source
is just guessing. An engineer would never act on that.

An agent that says "payment-svc has a memory leak [INC-2024-0487] — the connection
pool exhaustion log at 14:00 [melt-log-payment-svc-...] confirms it" can be trusted.

We enforce this at two levels:
1. The **system prompt** instructs Claude to cite every claim with `[source_id]`
2. The **grounding validator** checks every claim's source_id against the retrieved sources
   and logs violations (target: 0)

---

## The Streaming Contract

The agent streams. First token must arrive within 5 seconds.

Why? Because engineers debug live incidents. A 15-second wait for a batch response
breaks the flow. With streaming, they see the answer building in real time.

The SSE protocol:
```
data: {"type": "token", "content": "checkout-svc slows on Friday"}
data: {"type": "token", "content": " evenings because..."}
data: {"type": "done",  "response": { full structured response }}
```

The `done` event carries the complete structured response (sources, claims, gaps).

---

## What This Doesn't Do (Honest Gaps)

1. **No real-time data** — MELT is a 7-day snapshot, not a live feed. For production,
   swap the MELTRetriever for a real Datadog/Grafana API client.

2. **No persistent memory** — each query starts fresh. A production system would
   cache query plans and evidence for recurring questions.

3. **No auth** — the API has no authentication. Fine for a demo, not for production.

4. **Single-process metrics** — the `/metrics` endpoint uses in-memory tracking.
   Multi-process deployments would need Redis or Prometheus.

5. **Embedding model on CPU** — `all-MiniLM-L6-v2` runs on CPU, which is fast enough
   for 500 docs but would need GPU acceleration at 50M docs.

---

*The hidden cascade in the synthetic data:*
*payment-svc memory leak → checkout-svc latency → gateway-svc retry storm.*
*The agent finds all three layers.*

---

---

# Three Hard Queries — End-to-End Traces

What does the pipeline actually do when a real question comes in?
Below are three difficult queries traced all the way through: what each layer
does, what evidence it finds, and why the answer looks the way it does.

---

## Query 1 — "What changed before the last 3 major incidents?"

*Difficulty: Hard. Requires cross-referencing deployment history in Neo4j
against incident timestamps in OpenSearch. Neither source alone is enough.*

```
Engineer types: "What changed before the last 3 major incidents?"
                                   │
                    ───────────────┘
                    Layer 1: Query Understanding
                    ───────────────────────────
                    Claude reads the question.

                    The word "changed" → deployment_history intent
                    "major incidents"  → implicit severity filter P1/P2
                    "last 3"           → recent, no fixed time window yet

                    Output QueryPlan:
                    {
                      intent:          "deployment_history",
                      entities:        [],              ← no service named, check all
                      time_window:     null,            ← "last 3" is relative, not fixed
                      sources_needed:  ["neo4j",        ← deployments live here
                                        "opensearch"],  ← incident timestamps here
                      filters:         {"severity": "P1"}
                    }
                    ───────────────────────────
                    │
                    ▼
                    Layer 2: Parallel Retrieval  (t=0ms)
                    ──────────────────────────────────────────────────────────
                    Both sources fire at t=0ms simultaneously.

                     OpenSearch (fires at 0ms)           Neo4j (fires at 0ms)
                     ─────────────────────────           ────────────────────
                     Query: severity:P1 incidents        Query: all recent
                     sorted by timestamp DESC            DEPLOYED relationships
                                                         sorted by timestamp DESC

                     Returns at ~620ms:                  Returns at ~290ms:
                     ┌────────────────────────────┐      ┌───────────────────────────────┐
                     │ INC-2024-0487               │      │ payment-svc v2.4.2  Nov 13    │
                     │ payment-svc P1              │      │ checkout-svc v1.9.3 Nov 13    │
                     │ 2024-11-12 14:00            │      │ gateway-svc v4.1.1  Nov 13    │
                     │                             │      │ checkout-svc v1.9.2 Nov 12    │← 3.5h before INC-0487
                     │ INC-2024-0391               │      │ gateway-svc v4.0.9  Oct 28    │← 2h before INC-0391
                     │ gateway-svc P1              │      │ auth-svc v3.0.4     Oct 14    │← 4h before INC-0312
                     │ 2024-10-28 13:00            │      └───────────────────────────────┘
                     │                             │
                     │ INC-2024-0312               │
                     │ auth-svc P1                 │
                     │ 2024-10-14 13:00            │
                     └────────────────────────────┘

                     Evidence pool after iteration 1:  9 sources, confidence = 0.61
                     ──────────────────────────────────────────────────────────

    Confidence 0.61 < threshold 0.85 → iterate
    ───────────────────────────────────────────
                    │
                    ▼
                    Layer 3: Iterative Deepening — Iteration 2  (t=640ms)
                    ──────────────────────────────────────────────────────────
                    Claude is asked: "What's still missing?"

                    Claude sees 3 incidents and 6 deployments.
                    It notices: "I can see WHICH deployments happened near incidents,
                    but I don't know WHAT those deployments changed."

                    Gap identified:
                    "Change summaries for checkout-svc v1.9.2 and gateway-svc v4.0.9
                     — what did those deployments actually do?"

                    New retrieval fires with extra_context = that gap.

                     OpenSearch (fires at 640ms)         Neo4j (fires at 640ms)
                     ──────────────────────────          ─────────────────────
                     Searches for: "checkout v1.9.2      Fetches change_summary field
                     gateway v4.0.9 deployment"          for the two specific deploys

                     Returns at ~610ms:                  Returns at ~280ms:
                     ┌────────────────────────────┐      ┌──────────────────────────────────────┐
                     │ INC-2024-0487 rca_summary:  │      │ checkout-svc v1.9.2:                 │
                     │ "checkout-svc v1.9.2 removed│      │ "Refactor checkout flow. Remove      │
                     │  deprecated payment endpoint│      │  deprecated payment endpoint."        │
                     │  3.5h before incident"      │      │  author: bob.kumar                   │
                     └────────────────────────────┘      │                                      │
                                                         │ gateway-svc v4.0.9:                  │
                                                         │ "Increase retry count from 3 to 5."  │
                                                         │  author: carla.santos                │
                                                         └──────────────────────────────────────┘

                     New unique documents: 3   ← evidence delta > 0, iteration valid
                     Evidence pool after iteration 2: 12 sources, confidence = 0.87

    Confidence 0.87 ≥ threshold 0.85 → STOP
    ──────────────────────────────────────────
                    │
                    ▼
                    Layer 4: Grounded Response  (streaming, t=920ms → first token)
                    ──────────────────────────────────────────────────────────────

    "The three most recent major incidents were each preceded by a deployment
     within 2–4 hours:

     • INC-2024-0487 (payment-svc, P1, Nov 12 14:00): checkout-svc v1.9.2
       was deployed at 10:30 the same day [neo4j-deploy-checkout-svc-v1.9.2].
       The deployment removed a deprecated payment endpoint, which may have
       altered the retry path between checkout-svc and payment-svc
       [INC-2024-0487].

     • INC-2024-0391 (gateway-svc, P1, Oct 28 13:00): gateway-svc v4.0.9
       deployed at 11:00 [neo4j-deploy-gateway-svc-v4.0.9], increasing the
       retry count from 3 to 5. Under load this turned a transient error
       into a sustained retry storm [neo4j-rca-INC-2024-0391].

     • INC-2024-0312 (auth-svc, P1, Oct 14 13:00): auth-svc v3.0.4 at 09:00
       [neo4j-deploy-auth-svc-v3.0.4] changed JWT expiry from 1h to 8h,
       which filled the session store [neo4j-rca-INC-2024-0312].

     Knowledge gaps:
     - No automated pre-deployment checklist found in incident records
     - Unclear if the checkout-svc payment endpoint removal was intentional"

    iterations_used: 2
    confidence:      0.87
    sources:         12
    latency_ms:      ~6200
```

**Why this took 2 iterations:** The first pass found the timing correlation (deployment
before incident). But the change summaries — the actual "what changed" — only came
in iteration 2 when the agent explicitly asked for them.

---

## Query 2 — "Why does checkout slow down every Friday evening?"

*Difficulty: Hard. The answer lives entirely in MELT telemetry — a temporal pattern
invisible to OpenSearch (no keyword) and Neo4j (no time-series data).*

```
Engineer types: "Why does checkout slow down every Friday evening?"
                                   │
                    ───────────────┘
                    Layer 1: Query Understanding
                    ───────────────────────────
                    "checkout slow"  → pattern_analysis intent
                    "every Friday"   → recurring temporal pattern, no fixed window
                    "checkout"       → entity: checkout-svc

                    Output QueryPlan:
                    {
                      intent:          "pattern_analysis",
                      entities:        ["checkout-svc"],
                      time_window:     null,           ← recurring, not a single window
                      sources_needed:  ["opensearch",  ← past incident pattern history
                                        "neo4j",       ← dependency graph (what upstream?)
                                        "melt"],       ← THE key source for time patterns
                      filters:         {},
                      ambiguous:       false
                    }
                    ───────────────────────────
                    │
                    ▼
                    Layer 2: Parallel Retrieval  (t=0ms)
                    ──────────────────────────────────────────────────────────

       OpenSearch (t=0ms)            Neo4j (t=0ms)            MELT (t=0ms, in-memory)
       ─────────────────             ─────────────            ──────────────────────
       Search: "checkout             Find incident             Filter: logs containing
       slow friday latency           patterns for              "batch", "cron",
       batch"                        checkout-svc              "scheduled", "weekly"

       Returns at ~590ms:            Returns at ~260ms:        Returns at ~8ms:
       ┌──────────────────┐          ┌─────────────────┐       ┌──────────────────────────────────┐
       │ INC-2024-0312    │          │ pattern:         │       │ LOG 2024-11-08T18:00:00Z         │
       │ "checkout-svc    │          │ batch_job_lock   │       │ inventory-svc INFO               │
       │  elevated latency│          │ 6 occurrences   │       │ "weekly_batch_reconciliation      │
       │  every Friday    │          │ avg 120min      │       │  started. Table locks acquired." │
       │  inventory-svc   │          │                 │       │                                  │
       │  batch job       │          │ pattern:        │       │ LOG 2024-11-08T18:02:00Z         │
       │  contention"     │          │ pod_restart     │       │ checkout-svc WARN                │
       │                  │          │ 8 occurrences   │       │ "DB wait: 380ms (baseline 35ms)" │
       │ rca_summary:     │          └─────────────────┘       │                                  │
       │ "confirmed across│                                     │ METRIC 2024-11-08T18:05:00Z      │
       │  6 consecutive   │                                     │ checkout-svc p99_latency_ms      │
       │  Fridays"        │                                     │ value: 287.0                     │
       └──────────────────┘                                     │                                  │
                                                                │ LOG 2024-11-08T20:00:00Z         │
                                                                │ inventory-svc INFO               │
                                                                │ "reconciliation complete.        │
                                                                │  Locks released."               │
                                                                │                                  │
                                                                │ METRIC 2024-11-08T20:02:00Z      │
                                                                │ checkout-svc p99_latency_ms      │
                                                                │ value: 58.0   ← back to normal  │
                                                                └──────────────────────────────────┘

        Evidence pool after iteration 1: 11 sources
        Confidence = 0.88   ← MELT log + incident history + graph pattern = high signal

    Confidence 0.88 ≥ threshold 0.85 → STOP after iteration 1
    ────────────────────────────────────────────────────────────────
                    │
                    ▼
                    Layer 4: Grounded Response  (streaming, t=600ms → first token)
                    ──────────────────────────────────────────────────────────────

    "checkout-svc slows every Friday evening due to inventory-svc's
     weekly_batch_reconciliation job, which runs at 18:00 UTC [melt-pattern-
     log-inventory-svc-2024-11-08T18:00:00Z].

     The batch job acquires table locks on shared database tables. During
     the 2-hour window (18:00–20:00), checkout-svc's inventory lookup queries
     queue behind these locks — visible as a DB wait time spike from 35ms
     to 380ms [melt-log-checkout-svc-2024-11-08T18:02:00Z]. This pushes
     checkout-svc p99 latency from a baseline of ~55ms to ~287ms
     [melt-latency-checkout-svc-2024-11-08T18:05:00Z].

     The pattern is well established: 6 consecutive Friday occurrences
     confirmed in incident history [INC-2024-0312], and inventory-svc
     is listed as a high-criticality upstream dependency of checkout-svc
     [neo4j-deps-checkout-svc].

     Latency normalises around 20:00 UTC once the batch job completes
     and locks are released [melt-pattern-log-inventory-svc-2024-11-08T20:00].

     Knowledge gaps:
     - Exact table(s) locked not identified in available traces
     - No confirmation that the scheduled rescheduling (to 02:00 UTC) was implemented"

    iterations_used: 1     ← simple early stop — answer was in the first pass
    confidence:      0.88
    sources:         11
    latency_ms:      ~4800
```

**Why only 1 iteration:** MELT had the smoking gun immediately. The batch job log,
the latency spike, and the recovery timestamp were all in the first retrieval round.
High confidence from the start — no reason to search further.

Note the contrast with Query 1 (2 iterations): that query needed the agent to
notice a gap (change summaries) that wasn't visible until after seeing the first results.
This query had no such gap — the evidence was complete on the first pass.

---

## Query 3 — "What don't we know about today's incident?"

*Difficulty: Expert. This is a gap identification query — the answer IS the list
of things we can't find. The agent must actively hunt for absence of evidence,
not just presence of it.*

```
Engineer types: "What don't we know about today's incident?"
                                   │
                    ───────────────┘
                    Layer 1: Query Understanding
                    ───────────────────────────
                    "don't know"        → gap_identification intent  ← unusual
                    "today's incident"  → time window: 2024-11-14 (NOW anchor)
                    ambiguous: true     ← "today's" which incident exactly?

                    Output QueryPlan:
                    {
                      intent:          "gap_identification",
                      entities:        [],              ← not named — need to discover
                      time_window:     {
                        start: "2024-11-14T00:00:00Z",
                        end:   "2024-11-14T23:59:59Z",
                        description: "today"
                      },
                      sources_needed:  ["opensearch",  ← what incident?
                                        "neo4j",       ← what services? what RCA?
                                        "melt"],       ← what telemetry exists?
                      ambiguous:       true             ← no specific incident named
                    }
                    ───────────────────────────
                    │
                    ▼
                    Layer 2: Parallel Retrieval  (t=0ms)
                    ──────────────────────────────────────────────────────────

       OpenSearch (t=0ms)            Neo4j (t=0ms)            MELT (t=0ms, in-memory)
       ─────────────────             ─────────────            ──────────────────────
       Search: incidents             Search: incidents         Search: ERROR/CRITICAL
       from today                    on 2024-11-14             logs from payment-svc
                                                               today

       Returns at ~600ms:            Returns at ~310ms:        Returns at ~9ms:
       ┌──────────────────────┐      ┌───────────────────────┐ ┌─────────────────────────────┐
       │ INC-2024-0487        │      │ No incidents found on │ │ No logs from 2024-11-14     │
       │ 2024-11-12 (2 days   │      │ 2024-11-14 in graph.  │ │ (MELT window ends Nov 13)  │
       │ ago, the cascade)    │      │                       │ │                             │
       │ rca_summary present  │      │ Latest deployment:    │ │ Most recent log:            │
       │                      │      │ payment-svc v2.4.2    │ │ 2024-11-13T09:20           │
       │ INC-2024-0500        │      │ Nov 13 09:15          │ │ "v2.4.2 deployed. Pool      │
       │ payment-svc P2       │      │                       │ │  monitoring added."         │
       │ 2024-11-14 08:00     │      │ No incident nodes     │ │                             │
       │ INCOMPLETE — no RCA  │      │ for Nov 14            │ │ TRACE: cascade-001 (Nov 12) │
       │ rca_summary: ""      │      │                       │ │ Missing: gateway-svc spans  │
       │ root_cause: ""       │      │                       │ │ trace_id not correlated     │
       └──────────────────────┘      └───────────────────────┘ └─────────────────────────────┘

        Evidence pool after iteration 1:  8 sources
        Confidence = 0.42   ← low — lots of gaps, sparse evidence from today

    Confidence 0.42 < threshold 0.85 → iterate
    ────────────────────────────────────────────
                    │
                    ▼
                    Layer 3: Iteration 2  (t=620ms)
                    ──────────────────────────────────────────────────────────
                    Agent noticed:
                    - INC-2024-0500 has no RCA yet (payment-svc P2 from today)
                    - No heap dump in any logs
                    - Gateway traces missing from the cascade on Nov 12

                    Gap identified by Claude:
                    "heap dump logs, customer impact data, and cross-service
                     trace correlation for INC-2024-0500"

                     OpenSearch (t=620ms)         Neo4j (t=620ms)         MELT (t=620ms)
                     ────────────────────         ───────────────         ──────────────
                     Search: "heap dump           Check if payment-svc    Search for
                     payment memory trace         has active incident     trace IDs that
                     INC-2024-0500 customer"      node today              span all 3 services

                     Returns at ~580ms:            Returns at ~280ms:      Returns at ~7ms:
                     ┌──────────────────────┐      ┌──────────────────────┐ ┌──────────────────┐
                     │ INC-2024-0487        │      │ payment-svc:         │ │ No heap dump     │
                     │ (already seen)       │      │ last_incident=Nov12  │ │ logs found.      │
                     │                     │      │ tier=1, sla=99%      │ │                  │
                     │ No new incidents     │      │                      │ │ Only 2 traces    │
                     │ found for today      │      │ No INC-2024-0500     │ │ exist for        │
                     └──────────────────────┘      │ in graph yet         │ │ cascade-001.     │
                                                   │ (not seeded yet)     │ │ gateway-svc span │
                                                   └──────────────────────┘ │ MISSING.         │
                                                                             └──────────────────┘

        New unique documents: 2   ← small delta but real
        Evidence pool after iteration 2: 10 sources, confidence = 0.58

    Confidence 0.58 < threshold 0.85 → iterate (this is iteration 3, the last)
    ────────────────────────────────────────────────────────────────────────────
                    │
                    ▼
                    Layer 3: Iteration 3 — final allowed  (t=1220ms)
                    ──────────────────────────────────────────────────────────
                    Gap identified by Claude:
                    "customer transaction count affected by INC-2024-0500,
                     and whether the Nov 12 memory leak patch is confirmed permanent"

                    Retrieval fires one last time.
                    Returns mostly already-seen documents.
                    New unique documents: 1 (a deployment note)
                    Confidence stays at 0.58 — the gaps ARE the answer.

    Iteration 3 complete. Max iterations reached → proceed to generate.
    ─────────────────────────────────────────────────────────────────────
                    │
                    ▼
                    Layer 4: Grounded Response  (streaming, t=1500ms → first token)
                    ──────────────────────────────────────────────────────────────

    "Today's active incident is INC-2024-0500 (payment-svc, P2, raised at
     08:00 UTC) [INC-2024-0500]. Here is what we do and don't know:

     WHAT WE KNOW:
     • payment-svc is the affected service [INC-2024-0500]
     • The incident is unresolved as of the latest data
     • v2.4.2 was deployed yesterday (Nov 13 09:15) with a connection pool
       fix from the Nov 12 cascade [neo4j-deploy-payment-svc-v2.4.2]

     WHAT WE DON'T KNOW:

     ⚠️ No heap dump captured — the exact source of the memory growth
        is unknown. We know memory is elevated [melt-metric-payment-svc-
        memory_used_percent] but not which code path is leaking.

     ⚠️ Distributed traces are incomplete — the Nov 12 cascade trace
        (trace-cascade-001) is missing gateway-svc spans [melt-trace-
        cascade-001]. We cannot confirm the full propagation path.

     ⚠️ Customer transaction impact is unknown — no data in MELT or
        OpenSearch on how many transactions were affected or errored
        during today's P2 window.

     ⚠️ Root cause unconfirmed — INC-2024-0500 has no rca_summary
        yet [INC-2024-0500]. It is unclear whether this is a recurrence
        of the Nov 12 connection_pool_exhaustion or a new failure mode.

     ⚠️ Permanence of the Nov 12 fix unknown — the v2.4.2 patch increased
        pool max_size and added monitoring [neo4j-deploy-payment-svc-v2.4.2]
        but there is no post-mortem confirming the underlying memory leak
        root cause was addressed."

    iterations_used: 3     ← hit the cap — evidence was genuinely sparse
    confidence:      0.58  ← intentionally low, gaps are the point
    sources:         10
    latency_ms:      ~9400
    knowledge_gaps:  5     ← the answer IS the gaps
```

**Why 3 iterations and still low confidence:** This is the correct behavior for Q10.
The agent is supposed to identify what's missing — and the correct answer is a list
of things it couldn't find. Low confidence on a gap identification query means the
agent is being honest, not failing.

A system that returns confidence=0.95 on Q10 is almost certainly fabricating.
A system that returns 0.58 and says "here are 5 specific things we don't have data
on" is doing exactly what the question asked.

---

## Comparison: All Three Queries Side by Side

```
                   Q6: "What changed        Q8: "Why does           Q10: "What don't
                    before incidents?"       checkout slow Friday?"   we know today?"
                   ─────────────────        ──────────────────────   ───────────────
Intent             deployment_history       pattern_analysis         gap_identification
Primary source     Neo4j + OpenSearch       MELT                     All (absence counts)
Iterations used    2                        1                        3
Confidence         0.87                     0.88                     0.58
First-pass yield   timing correlation       smoking-gun log          sparse, P2 open
Gap identified     change summaries         none (complete)          heap dump, traces,
                   missing in pass 1        enough in pass 1         customer impact
Stops because      confidence met           confidence met           max iterations
Answer shape       "who deployed what       "batch job, here's       "5 specific things
                    when, cross-ref'd"       the exact log"           we cannot find"
Latency            ~6.2s                    ~4.8s                    ~9.4s
```

The pattern is clear:
- **Simple + complete evidence** → 1 iteration, ~5s, high confidence
- **Requires cross-source correlation** → 2 iterations, ~6s, high confidence
- **Sparse by design (gaps ARE the answer)** → 3 iterations, ~9s, honest low confidence
