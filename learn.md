# Architecture & Library Choices

## Problem Statement

Enterprise engineering teams running BugRaid already get automated root cause analysis —
but that only covers what the system already knows to look for. When a production incident
is ambiguous or novel, engineers need to research: ask freeform questions, correlate signals
across deployments, service dependencies, and live telemetry, and get answers they can act
on immediately. The existing tooling forces them to query three separate systems manually
and stitch the results together themselves, which costs time they don't have during an
outage. This agent replaces that with a single interface that searches all three sources
in parallel, iterates until it has enough evidence, and streams a grounded answer with
every claim cited back to the data it came from.

## How it works

The system is a 4-layer pipeline. Each layer has one job and hands off to the next.

```
Engineer's question
       ↓
[Layer 1] Query Understanding    — Claude parses intent + entities into a structured plan
       ↓
[Layer 2] Parallel Retrieval     — OpenSearch + Neo4j + MELT fire simultaneously
       ↓
[Layer 3] Iterative Deepening    — up to 3 rounds, each adds new evidence
       ↓
[Layer 4] Response Generator     — Claude synthesizes, streams tokens, cites every claim
       ↓
SSE stream to the engineer
```

---

## Why each layer exists

**Layer 1 — Query Understanding**
Natural language is ambiguous. "checkout is slow" doesn't tell you whether to look at
deployments, incidents, or metrics. Claude turns that into a structured QueryPlan with
intent, entities, time window, and which sources to hit. This keeps the retrieval layer
clean — it never has to guess.

**Layer 2 — Parallel Retrieval**
Three sources, fired with asyncio.gather. If you do them sequentially you're looking at
~1.3s just waiting. Parallel drops it to ~800ms (the slowest one wins). Also: if
OpenSearch is down mid-restart, Neo4j and MELT still return — return_exceptions=True
handles that.

**Layer 3 — Iterative Deepening**
Single-pass RAG misses things. The first retrieval might surface the gateway-svc retry
storm (the noisy symptom) but not the payment-svc memory leak (the root cause). Round 2
asks Claude "what's still missing?" and uses that gap to search again. Stops early if
confidence crosses 0.85 or no new docs come back.

**Layer 4 — Response Generator**
Two hard rules: every claim cites a [source_id], and the response streams token-by-token.
Engineers debugging a live incident can't wait 30 seconds for a batch response.

---

## Library choices

**LangGraph**
The assessment required it explicitly (LangChain chains = auto-reject). But it's also the
right call: the pipeline has conditional logic (stop iterating early? retry?), shared
mutable state, and branching. LangGraph models that as a state machine. LangChain chains
can't do early exits cleanly.

**Claude (Anthropic)**
Required by the assessment. Used in 3 places: query parsing, gap identification during
iteration, and response generation. Model: claude-sonnet-4-20250514.

**fastembed**
Originally the code used sentence-transformers but that pulls in PyTorch. On the dev
machine (macOS 26, PyTorch 2.8) the C++ mutex crash killed the process on import.
fastembed runs the same all-MiniLM-L6-v2 model via ONNX runtime — no PyTorch, no crash,
same 384-dim vectors. On Windows it doesn't matter, but ONNX is lighter anyway.

**OpenSearch**
Hybrid search: dense k-NN + BM25 merged via Reciprocal Rank Fusion. BM25 is good at exact
matches like "connection_pool_exhaustion". Dense vectors catch semantic matches like
"memory leak ≈ OOM error". RRF combines both without needing to tune weights — documents
that rank high in both lists float to the top.

**Neo4j**
Graph database for service dependencies. You can't answer "what does checkout-svc depend
on and what's their health?" with a relational DB without ugly joins. In Neo4j it's a
3-line Cypher traversal. Raw Cypher used throughout — no ORM, so queries are readable
and debuggable.

**FastAPI + SSE**
FastAPI for async support and automatic OpenAPI docs. SSE (Server-Sent Events) over
WebSocket because SSE is unidirectional (server → client), simpler to implement, and works
with "curl -N" out of the box. The first Claude token needs to arrive in under 5 seconds —
streaming makes that possible without holding a full response in memory.

**Pydantic**
Data validation at every layer boundary. If Claude returns malformed JSON, Pydantic catches
it before it propagates. Every QueryPlan, Source, Claim, and ResearchResponse is validated
at construction time.

---

## What's not in the stack (and why)

**No caching** — every evaluation question is unique, so a cache would have 0% hit rate.
Added complexity for no gain.

**No Langfuse** — the assessment listed it as optional. The /metrics endpoint covers what
the assessors actually check.

**No auth** — explicitly out of scope per the spec.

**MELT as JSON in memory** — avoids spinning up a 4th database (Prometheus, Jaeger). The
7-day telemetry window is small enough to fit in RAM and filter fast.

---

## Latency budget and how we stay under 5 seconds

The 5-second target is "first token reaches the engineer". Everything before that is
on the critical path.

**Where the time goes (worst case, 3 iterations):**
```
Layer 1: Claude parses query intent       ~600ms   (serial — nothing starts until done)
Layer 2: Parallel retrieval               ~700ms   (OpenSearch dominates)
Layer 3: Gap call (Haiku) + retrieval     ~400ms + 700ms = ~1.1s  ×2 = ~2.2s
Layer 4: Claude TTFT (first token)        ~1.2s
─────────────────────────────────────────────────────────────────────────────
Worst case before first token:            ~4.7s
```

**Three optimisations applied:**

1. **Haiku for utility calls (Layer 1 + Layer 3 gap identification)**
   Query parsing and gap finding are short structured tasks. Haiku handles them in
   ~150ms vs ~600ms for Sonnet. Only the final response generation uses Sonnet.
   Saves ~450ms per gap call, ~900ms total on a 3-iteration query.

2. **Overlap Layer 1 and Layer 2**
   Instead of waiting for the query plan before starting retrieval, we fire a broad
   retrieval (all sources, raw query text) at the same time as query understanding.
   By the time the plan arrives, round-1 evidence is already back.
   Saves ~600ms off the critical path.

3. **Cap iterations at 1 for simple intents**
   Deployment history, dependency analysis, and service health queries have a single
   well-defined answer. Running 3 iterations on them wastes ~2s. We cap them at 1
   and let the confidence threshold handle everything else.

**Revised critical path after optimisations:**
```
Layer 1 + 2 in parallel:  max(~150ms, ~700ms)  = ~700ms
Layer 3 (complex only):   ~400ms + ~700ms       = ~1.1s  (1 extra round)
Layer 4 TTFT:             ~1.2s
─────────────────────────────────────────────────────────────────────────────
Complex queries (2 iterations):  ~3.0s   ✓
Simple queries (1 iteration):   ~2.1s   ✓
```

---

## Loom Demo Notes

### 3:00–4:00 — Iterative deepening in action

No setup needed. Complex queries now always run at least 2 iterations — the confidence
check only kicks in from round 3 onwards.

Run Q10 with the uvicorn terminal visible:

```bash
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "What don'\''t we know about today'\''s incident?", "question_id": "Q10"}'
```

You should see in the logs:

```
INFO | After iteration 1: 8 sources, confidence=0.94
INFO | Identified gap for iteration: missing failure mode details for today's active incident
INFO | Iteration 2: found 4 new sources
INFO | After iteration 2: 12 sources, confidence=0.96
INFO | Confidence 0.96 >= threshold — stopping at iteration 2
```

**What to say while pointing at the logs**

Round 1 has enough to answer — confidence is already high. But we always do a second
pass because round 1 only sees what it can find with the raw query. Round 2 knows what
round 1 found, asks "what's still missing?", and searches for that specifically. It found
4 new documents the first pass missed. Then it stops — confidence threshold passed, no
more calls.

Then run a deployment history query immediately after:

```bash
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "What was deployed to payment-svc last week?", "question_id": "Q_dep"}'
```

Logs show `1 iterations` — intent is `deployment_history`, capped at 1. Two queries,
two different iteration counts. The system is deciding, not just looping a fixed number
of times.

---

### 4:00–5:00 — What to build next: Verified Resolution Capture

**The problem it solves**

Right now the agent retrieves from historical incident docs. Those docs are written
by whoever filed the ticket — which means they describe what engineers *thought* was
wrong, not what actually fixed the incident. At scale, that distinction compounds.
If 1,000 incidents have slightly wrong RCAs, every future retrieval is pulling from
bad signal.

**What Verified Resolution Capture does**

After an incident closes, the agent re-runs the original research query against what
actually happened. It diffs its recommendation against the actual fix. If they match,
it creates a `ResolutionPattern` node in Neo4j with a `verified: true` flag and 3x
retrieval weight. If they don't match, it flags the gap for review.

The confirmation is one click — the on-call engineer sees "did this match your fix?"
after the incident closes. They don't have to write anything.

```
Incident closes
      ↓
Agent re-runs query → compares recommendation vs actual fix
      ↓
Match?  → ResolutionPattern node (verified=true, weight=3x)
No match? → Gap flagged, incident doc corrected
      ↓
Next similar incident retrieves the verified pattern first
```

**Why it scales**

At 100 engineers: the verified pattern library is small but every entry is trustworthy.
At 10,000 engineers: the library has seen thousands of incident variations. The agent
stops recommending from unverified docs when a verified pattern exists for that
failure mode.
At 100,000 engineers: the verified patterns cover essentially every common failure
mode across the industry. New incidents match to verified fixes in round 1, confidence
crosses 0.85 immediately, iteration stops. The system gets faster as it gets bigger —
the opposite of most RAG systems which degrade under corpus growth because more docs
means more noise.

The Neo4j hook is already there. `ResolutionPattern` nodes connect to the same
`Service` and `Incident` nodes already in the graph. The retrieval weight is a
one-line change to the Cypher query. The hard part — building the pipeline that
produces verified signal at scale — is what makes this defensible.
