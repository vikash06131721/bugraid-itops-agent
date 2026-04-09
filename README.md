# BugRaid ITOps Research Agent

Ask anything about your production environment, get a grounded answer with sources cited.
Queries OpenSearch, Neo4j, and MELT telemetry in parallel and streams the response as it generates.

---

## Running it

**Prerequisites:** Docker, Python 3.11+, Anthropic API key

```bash
# 1. Start OpenSearch and Neo4j
docker-compose up -d

# Wait ~30s, then confirm OpenSearch is up
curl http://localhost:9200/_cluster/health

# 2. Install dependencies
pip install -e ".[dev]"
pip install fastembed aiohttp

# 3. Set your API key
cp .env.example .env
# edit .env and fill in ANTHROPIC_API_KEY

# 4. Seed the data (only needed once)
python scripts/generate_data.py
python scripts/seed_stores.py

# 5. Start the server
uvicorn src.api.main:app --reload
```

---

## Querying

```bash
# Ask anything
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "Why does checkout slow down every Friday evening?", "question_id": "Q8"}'

# Health check
curl http://localhost:8000/health

# Metrics (latency, cost, source hit rates)
curl http://localhost:8000/metrics
```

---

## Tests

```bash
# Unit tests — no Docker needed
pytest tests/ -k "not evaluation" -v

# Full evaluation against all 10 test questions
python run_evaluation.py --questions all --output my_answers.json --compare-expected
```

---

See [docs/arch.md](docs/arch.md) for architecture and [docs/tradeoffs.md](docs/tradeoffs.md) for design decisions.
