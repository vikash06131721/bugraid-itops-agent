# BugRaid ITOps Research Agent

An engineer asks: *"Why does checkout slow down every Friday evening?"*

The agent queries three data sources in parallel, deepens the search up to 3 rounds,
and streams a grounded answer with every claim cited.

---

## Architecture

```
Engineer Query
      │
      ▼
[Layer 1] Query Understanding (Claude)
      → Parses intent, entities, time window, source routing
      │
      ▼
[Layer 2] Parallel Retrieval (asyncio.gather)
      ├── OpenSearch  — 500 incidents, hybrid dense+BM25
      ├── Neo4j       — service graph, deployments, dependencies
      └── MELT        — 7-day telemetry (metrics, logs, traces)
      │
      ▼
[Layer 3] Iterative Deepening (up to 3 rounds)
      → Each round adds new evidence or stops early
      │
      ▼
[Layer 4] Grounded Streaming Response (Claude)
      → Every claim cites [source_id]. Gaps flagged explicitly.
      → SSE stream, first token < 5s
```

Full architecture walkthrough with diagrams: [docs/arch.md](docs/arch.md)

---

## Setup

### Prerequisites
- Docker + Docker Compose
- Python 3.11+
- An Anthropic API key

### 1. Start infrastructure (one command)

```bash
docker-compose up -d

# Verify OpenSearch is ready
curl http://localhost:9200/_cluster/health
# Should return: "status":"green" or "status":"yellow"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Install dependencies

```bash
pip install -e ".[dev]"
```

### 4. Generate and seed data

```bash
python scripts/generate_data.py    # creates data/*.jsonl, *.cypher, *.json
python scripts/seed_stores.py      # loads data into OpenSearch and Neo4j
```

### 5. Start the agent

```bash
uvicorn src.api.main:app --reload
```

---

## Usage

### Query (streaming SSE)

```bash
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "Why does checkout slow down every Friday evening?", "question_id": "Q8"}'
```

### Health check

```bash
curl http://localhost:8000/health
```

### Metrics

```bash
curl http://localhost:8000/metrics
```

---

## Running the Evaluation

```bash
# Run all 10 test questions
python run_evaluation.py --questions all --output my_answers.json

# Run specific questions
python run_evaluation.py --questions Q1,Q5,Q10

# Compare against expected outputs (auto-scores Q1-Q5)
python run_evaluation.py --questions all --compare-expected
```

---

## Running Tests

```bash
# Unit tests only (no docker needed)
pytest tests/ -k "not evaluation" -v

# With coverage
pytest tests/ -k "not evaluation" --cov=src --cov-report=term-missing

# Full integration tests (requires docker-compose up + seeded data)
BUGRAID_INTEGRATION=true pytest tests/test_evaluation.py -v
```

---

## The 10 Test Questions

| # | Question | Difficulty | Primary Source |
|---|----------|------------|----------------|
| Q1 | What is the payment service responsible for? | Low | OpenSearch |
| Q2 | What deployments happened in the last 24 hours? | Low | Neo4j |
| Q3 | Which services does checkout-svc depend on? | Low | Neo4j traversal |
| Q4 | What incidents involved auth-svc last month? | Medium | OpenSearch + Neo4j |
| Q5 | Is payment-svc healthy right now? | Medium | MELT + Neo4j |
| Q6 | What changed before the last 3 major incidents? | Hard | Neo4j + OpenSearch |
| Q7 | Which service is the most fragile in our system? | Hard | Graph + MELT |
| Q8 | Why does checkout slow down every Friday evening? | Hard | Temporal MELT |
| Q9 | Compare how we resolved the last 5 payment incidents | Very Hard | Multi-doc synthesis |
| Q10 | What don't we know about today's incident? | Expert | Gap identification |

---

## Design Choices

**Why LangGraph?**
The pipeline is a state machine with conditional iteration — not a linear chain.
LangGraph models this directly. LangChain chains would require awkward workarounds.

**Why `all-MiniLM-L6-v2`?**
Local (no API cost), 80MB, 384-dimensional, proven for semantic similarity on short
technical texts. Good enough for 500 documents. Full justification: [docs/tradeoffs.md](docs/tradeoffs.md)

**Why hybrid search?**
BM25 catches exact technical terms (`connection_pool_exhaustion`).
Dense search catches semantic similarity (`OOM` ≈ `memory leak`).
RRF merges both without learned parameters.

**Why parallel retrieval?**
Sequential would take ~900ms. Parallel takes ~600ms (slowest source wins).
Timing logs prove this — verifiable in stdout.

---

## Trade-offs

Full honest accounting in [docs/tradeoffs.md](docs/tradeoffs.md). Key cuts:

- **No caching** — hit rate would be ~0% on 10 unique eval questions
- **In-memory MELT** — avoids extra infra, sufficient for 7-day snapshot
- **In-memory metrics** — loses data on restart, fine for demo
- **No auth** — the spec says `curl localhost:8000/query` must work

---

## Known Limitations

- MELT data is a static snapshot — not real-time telemetry
- Confidence scoring is heuristic (relevance scores + diversity), not calibrated
- `/metrics` uses in-memory tracking — data resets on restart
- `all-MiniLM-L6-v2` runs on CPU — fine for 500 docs, needs GPU at 50M

---

## What I'd Build Next

**Semantic query caching.** "Is payment-svc healthy?" gets asked dozens of times per hour.
Cache the evidence pool by nearest-neighbor query embedding similarity. Invalidate
on new incidents or deployments. Would cut latency from ~5s to <100ms for cached queries.

---

## Project Structure

```
bugraid-itops-agent/
├── docker-compose.yml          # OpenSearch + Neo4j, one command
├── pyproject.toml
├── data/
│   ├── synthetic_incidents.jsonl   # 500 incidents, 6 services, 60 days
│   ├── neo4j_seed.cypher           # 200 service nodes + relationships
│   ├── melt_telemetry.json         # 7-day telemetry with cascade anomaly
│   ├── expected_outputs.json       # Ground truth for 10 questions
│   └── rca_schema.json             # JSON schema for response validation
├── scripts/
│   ├── generate_data.py            # Creates all synthetic data
│   └── seed_stores.py              # Loads data into OpenSearch + Neo4j
├── src/
│   ├── models.py                   # All shared data models (QueryPlan, Source, etc.)
│   ├── agents/
│   │   ├── query_understanding.py  # Layer 1: NL → QueryPlan
│   │   ├── retrieval_orchestrator.py # Layer 2: parallel fetch
│   │   ├── iterative_deepening.py  # Layer 3: up to 3 rounds
│   │   └── response_generator.py  # Layer 4: grounded streaming
│   ├── retrieval/
│   │   ├── opensearch_retriever.py # Hybrid dense+BM25
│   │   ├── neo4j_retriever.py      # Raw Cypher queries
│   │   └── melt_retriever.py       # In-memory telemetry filter
│   ├── api/
│   │   └── main.py                 # FastAPI: POST /query, GET /metrics
│   └── utils/
│       ├── grounding.py            # Source validation, confidence scoring
│       └── metrics.py              # Latency, cost, hit rate tracking
├── tests/
│   ├── conftest.py
│   ├── test_query_understanding.py # Layer 1: 10/10 queries parsed correctly
│   ├── test_retrieval.py           # Layer 2: parallel verified by timing
│   ├── test_iteration.py           # Layer 3: evidence delta > 0 per round
│   ├── test_grounding.py           # Layer 4: 0 ungrounded claims
│   └── test_evaluation.py          # All 10 questions, schema validation
├── run_evaluation.py               # python run_evaluation.py --questions all
└── docs/
    ├── arch.md                     # Architecture walkthrough (plain English)
    └── tradeoffs.md                # Honest design trade-offs
```
