# Trade-offs and Design Decisions

Honest accounting of what I chose, what I cut, and why.

---

## Embedding Model: all-MiniLM-L6-v2

**Chose:** `sentence-transformers/all-MiniLM-L6-v2`
**Alternatives considered:** OpenAI `text-embedding-3-small`, Cohere Embed, `bge-large-en`

**Why this one:**
- Runs locally — no API latency, no cost per embedding, no rate limits
- 80MB model size — downloads once, stays in memory
- 384 dimensions — small enough for fast k-NN search, good enough for incident text
- Proven for semantic similarity on short to medium texts (incident titles, RCA summaries)

**What you give up:**
- OpenAI `text-embedding-3-small` produces better embeddings on complex prose
- `bge-large-en` is stronger on technical text but is 1.3GB and requires GPU for good throughput
- At 50M documents, the index itself becomes the bottleneck before the model quality matters

**The call:** For 500 incident documents on a single machine, local + fast wins.

---

## LangGraph Over LangChain

**This was a hard requirement** (LangChain chains = auto-fail per the spec).

But I'd have chosen LangGraph anyway. The reason: this pipeline is a state machine,
not a chain. The "should we iterate again?" decision is conditional, the state
accumulates across iterations, and the flow can branch. LangGraph models this
naturally. LangChain chains model it awkwardly.

**What I simplified:** I don't use LangGraph's built-in checkpoint/persistence features.
For this demo, the state lives in memory for the duration of one query. Adding
checkpointing (for retry on crash, or for multi-turn research sessions) would be
the obvious next step.

---

## Hybrid Search: Dense + BM25 via RRF

**Chose:** k-NN (all-MiniLM embeddings) + BM25, merged via Reciprocal Rank Fusion

**Why not just BM25?**
Because "connection pool exhaustion" and "OOM caused by connection leak" are the same
problem but share no keywords. Dense search bridges that gap.

**Why not just dense?**
Because BM25 is unbeatable for exact matches: incident IDs, service names, specific
error strings like `JWKS_ENDPOINT_TIMEOUT`. Dense search dilutes these.

**Why RRF?**
Because it's simple, parameter-free (k=60 is the standard constant), and works well
in practice. The alternative — learned score fusion — requires labeled data we don't have.

---

## MELT as In-Memory JSON

**Chose:** Load `melt_telemetry.json` into memory at startup
**Alternative:** Run a local Prometheus + Grafana stack

**Why in-memory:**
- Zero additional infrastructure for the assessor to spin up
- The data is a 7-day snapshot — it doesn't need to be queryable in real time
- Filtering a few thousand rows in Python is fast enough (< 10ms)

**What you give up:**
- Can't add new telemetry without restarting the agent
- No real-time alerting integration
- Doesn't simulate the actual query patterns you'd use with Datadog/Grafana APIs

---

## Streaming: FastAPI SSE Over WebSocket

**Chose:** Server-Sent Events (`text/event-stream`)
**Alternative:** WebSocket

**Why SSE:**
- Works over HTTP/1.1 and HTTP/2 without upgrade negotiation
- Simpler to test with `curl -N`
- Reconnection is handled automatically by browsers and SSE clients
- The agent sends data in one direction (server → client) — bidirectional WebSocket is overkill

**What you give up:**
- WebSocket allows the client to cancel a long-running query mid-stream
- WebSocket is better for interactive "conversation" UX

---

## Observability: In-Memory Metrics Tracker

**Chose:** Simple in-memory `MetricsTracker` singleton
**Alternative:** Langfuse, OpenTelemetry, Prometheus

**Why in-memory:**
- Zero setup for the assessor — no Langfuse account, no Prometheus scraper
- The `/metrics` endpoint returns the right shape of data (exactly what the spec requires)
- Good enough to demonstrate the observability intent

**What you give up:**
- Metrics disappear on restart
- Can't alert on metrics
- Multi-process deployments (gunicorn workers) each have their own tracker

**The honest note:** In production, every LLM call should be traced through Langfuse
or a similar tool. I'd add that as the first post-submission improvement.

---

## No Caching

**Cut:** Query result caching

**Why cut it:**
- The 10 test questions are all different — cache hit rate would be ~0% on this eval
- Caching adds complexity: invalidation logic, stale evidence, cache key design
- The assessors check that iterative deepening actually runs — caching might hide that

**When it would matter:**
- Production with thousands of engineers — "Is payment-svc healthy?" gets asked 50 times/hour
- Semantic caching (nearest-neighbor match of query embeddings) would be the right approach

---

## No Authentication

**Cut:** API authentication

**Why cut it:**
- The spec says "docker-compose up" and "curl localhost:8000/query" — auth would break that
- Adds nothing to the research quality being evaluated

**What you'd add in production:**
- JWT-based auth tied to the engineer's SSO identity
- Per-engineer query quotas
- Audit logging of all queries (who asked what, when, what the answer was)

---

## Confidence Scoring: Heuristic, Not Calibrated

**Chose:** Heuristic confidence (weighted average of source relevance scores + diversity bonus)
**Alternative:** Calibrated probabilistic confidence from a separate model

**Why heuristic:**
- Calibration requires held-out labeled data — we don't have that
- The heuristic is transparent and debuggable
- It correctly signals when evidence is thin (low scores, few sources)

**The known weakness:**
- The confidence heuristic can be overconfident when many low-quality sources agree
- It can be underconfident when one very high-quality source disagrees with others
- A calibrated model (Platt scaling, isotonic regression) would fix this with enough data
