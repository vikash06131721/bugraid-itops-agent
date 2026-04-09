# BugRaid ITOps Agent — Setup Instructions & Troubleshooting Log

Everything we ran this morning, every error we hit, and every fix we applied.
Follow this top to bottom on a fresh machine.

---

## Environment

| Thing | Value |
|---|---|
| Machine | MacBook (Apple Silicon, macOS 26.2) |
| Python env | `/Users/vikashprasad/Desktop/new_code_nlp/tf-env` (Python 3.10.18) |
| Project root | `/Users/vikashprasad/Desktop/bugraidai/bugraid-itops-agent/` |
| Anthropic key | stored in `/Users/vikashprasad/Desktop/bugraidai/a-key.json` |
| Docker | v27.4.0 |
| Docker Compose | v2.39.4 |

Always use the tf-env Python, not system Python:
```bash
/Users/vikashprasad/Desktop/new_code_nlp/tf-env/bin/python
/Users/vikashprasad/Desktop/new_code_nlp/tf-env/bin/pip
/Users/vikashprasad/Desktop/new_code_nlp/tf-env/bin/uvicorn
```

Or activate it first:
```bash
source /Users/vikashprasad/Desktop/new_code_nlp/tf-env/bin/activate
```

---

## Step-by-Step Setup (Clean Machine)

### 1. Install dependencies

```bash
cd /Users/vikashprasad/Desktop/bugraidai/bugraid-itops-agent
pip install -e ".[dev]"
pip install fastembed   # critical — see Error #2 below
```

### 2. Copy and configure environment

```bash
cp .env.example .env
# .env already contains the API key — no manual edit needed
```

### 3. Generate synthetic data

```bash
python scripts/generate_data.py
```

Expected output:
```
Generating synthetic data...
  → 500 incidents...
     ✓ 502 incidents written
  → Neo4j seed script...
     ✓ neo4j_seed.cypher written
  → MELT telemetry (7 days × 6 services, 5-min intervals)...
     ✓ 52416 metrics, 22 logs, 5 traces
  → Expected outputs for 10 test questions...
     ✓ expected_outputs.json written
     ✓ rca_schema.json written
```

### 4. Start Docker (OpenSearch + Neo4j)

```bash
docker-compose up -d
```

Wait ~30 seconds, then verify both are healthy:
```bash
curl http://localhost:9200/_cluster/health
# Expected: "status":"yellow" or "status":"green"

docker ps
# Both bugraid-opensearch and bugraid-neo4j should show (healthy)
```

### 5. Seed the data stores

```bash
python scripts/seed_stores.py
```

Expected output:
```
[OpenSearch] Starting...
  Loading embedding model via fastembed (ONNX, no PyTorch)...
  Embedding model loaded ✓
  Created index 'bugraid-incidents' with kNN + BM25 mappings
  Embedded 502/502...
  Indexed 500 documents, 0 failed
[OpenSearch] Done ✓

[Neo4j] Starting...
  Connected to Neo4j
  Running N Cypher statements...
  Graph nodes: {'Service': 199, 'Incident': 118, ...}
[Neo4j] Done ✓
```

Verify seeding worked:
```bash
# Should return {"count":500,...}
curl -s http://localhost:9200/bugraid-incidents/_count

# Should show Service, Incident, Deployment, ResolutionPattern nodes
curl -s -u neo4j:bugraidpassword \
  -H "Content-Type: application/json" \
  -d '{"statements":[{"statement":"MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC"}]}' \
  http://localhost:7474/db/neo4j/tx/commit
```

### 6. Start the API

```bash
uvicorn src.api.main:app --reload
```

Wait for `Application startup complete.` in the logs.

### 7. Test it

Open a new terminal tab:

```bash
# Health check
curl http://localhost:8000/health

# Streaming query (Q5 — service health)
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "Is payment-svc healthy?", "question_id": "Q5"}'

# Friday pattern (Q8 — hard, temporal)
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "Why does checkout slow down every Friday evening?", "question_id": "Q8"}'

# Metrics dashboard
curl http://localhost:8000/metrics

# Gap identification (Q10 — expert)
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "What dont we know about todays incident?", "question_id": "Q10"}'
```

### 8. Run unit tests (no Docker needed)

```bash
pytest tests/ -k "not evaluation" -v
# Expected: 37 passed in ~3s
```

### 9. Run full evaluation

```bash
python run_evaluation.py --questions all --output my_answers.json --compare-expected
```

---

## Errors Encountered & Fixes

---

### Error #1 — Wrong Python version

**Error:**
```
ERROR: Package 'bugraid-itops-agent' requires a different Python: 3.10.18 not in '>=3.11'
```

**Cause:** `pyproject.toml` specified `requires-python = ">=3.11"` but tf-env has Python 3.10.

**Fix:** Changed `pyproject.toml`:
```toml
# Before
requires-python = ">=3.11"

# After
requires-python = ">=3.10"
```

---

### Error #2 — macOS libc++ mutex crash (the big one)

**Error:**
```
libc++abi: terminating due to uncaught exception of type std::__1::system_error:
mutex lock failed: Invalid argument
zsh: abort      python scripts/seed_stores.py
```

**Cause:** PyTorch 2.8.0 on macOS 26.2 crashes when importing `sentence-transformers`
(and even `transformers` directly). The Rust tokenizer library initialises OS mutexes
that are incompatible with macOS 26's libc++. Setting `TOKENIZERS_PARALLELISM=false`
and `OMP_NUM_THREADS=1` before import does not help — the crash happens at the C++
library constructor level before any Python code runs.

**Attempted fixes that did NOT work:**
- `os.environ["TOKENIZERS_PARALLELISM"] = "false"` before import — still crashes
- `os.environ["OMP_NUM_THREADS"] = "1"` — still crashes
- Using `transformers.AutoModel` directly (bypassing `sentence-transformers`) — still crashes (same PyTorch dependency)

**Final fix:** Replaced `sentence-transformers` (PyTorch-based) with `fastembed`
(ONNX runtime-based). Same model (`all-MiniLM-L6-v2`), same 384-dimensional
output vectors, no PyTorch, no crash.

```bash
pip install fastembed
```

Changed in `src/retrieval/opensearch_retriever.py`:
```python
# BEFORE (crashes on macOS 26 + PyTorch 2.8)
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")
vector = model.encode(text, normalize_embeddings=True).tolist()

# AFTER (works — ONNX runtime, no PyTorch)
from fastembed import TextEmbedding
model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
vector = list(next(iter(model.embed([text]))))
```

Same change applied in `scripts/seed_stores.py`.

---

### Error #3 — `setuptools.backends` not found

**Error:**
```
ModuleNotFoundError: No module named 'setuptools.backends'
```

**Cause:** `pyproject.toml` had `build-backend = "setuptools.backends.legacy:build"`
which requires a newer setuptools than what's in tf-env.

**Fix:**
```toml
# Before
build-backend = "setuptools.backends.legacy:build"

# After
build-backend = "setuptools.build_meta"
```

---

### Error #4 — Test collection hang

**Symptom:** `pytest tests/` hung indefinitely at `collecting ...` with no output.

**Cause:** `sentence_transformers` was imported at module level in
`src/retrieval/opensearch_retriever.py`. On macOS 26, even importing it
(without calling anything) triggers the PyTorch C++ constructor crash,
killing the process before pytest could collect tests.

**Fix:** Made the import lazy — moved inside the `_load_model()` function
which is only called at runtime, not at import time. Later replaced entirely
with fastembed (Error #2 fix).

---

### Error #5 — OpenSearch connection refused during seeding

**Error:**
```
ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 9200)
```

**Cause:** `python scripts/seed_stores.py` was run before `docker-compose up -d`,
or Docker containers hadn't finished starting yet.

**Fix:** Wait for containers to be healthy before seeding:
```bash
docker-compose up -d
# Wait ~30 seconds
curl http://localhost:9200/_cluster/health   # must return before seeding
python scripts/seed_stores.py
```

---

### Error #6 — API not running when curl-ing port 8000

**Error:**
```
curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server
```

**Cause:** The uvicorn API server wasn't started yet.

**Fix:** Start the API in a terminal first, wait for startup complete, then query:
```bash
# Terminal 1
uvicorn src.api.main:app --reload
# Wait for: INFO: Application startup complete.

# Terminal 2
curl -N http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "Is payment-svc healthy?", "question_id": "Q5"}'
```

---

## Quick Reference — All Commands

```bash
# 1. Go to project
cd /Users/vikashprasad/Desktop/bugraidai/bugraid-itops-agent

# 2. Start infra
docker-compose up -d

# 3. Wait for health
curl http://localhost:9200/_cluster/health

# 4. Generate data (only needed once)
python scripts/generate_data.py

# 5. Seed stores (only needed once, or after docker-compose down -v)
python scripts/seed_stores.py

# 6. Start API
uvicorn src.api.main:app --reload

# 7. Unit tests (new terminal, no docker needed)
pytest tests/ -k "not evaluation" -v

# 8. Full evaluation
python run_evaluation.py --questions all --output my_answers.json

# 9. Tear down Docker when done
docker-compose down
```

---

## Data Verification Commands

```bash
# OpenSearch — should return 500
curl -s http://localhost:9200/bugraid-incidents/_count | python -m json.tool

# Neo4j — node counts
curl -s -u neo4j:bugraidpassword \
  -H "Content-Type: application/json" \
  -d '{"statements":[{"statement":"MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC"}]}' \
  http://localhost:7474/db/neo4j/tx/commit | python -m json.tool

# API health
curl http://localhost:8000/health

# API metrics (after running at least one query)
curl http://localhost:8000/metrics | python -m json.tool
```

---

## Key Files

| File | Purpose |
|---|---|
| `.env` | API key + connection strings (already configured) |
| `docker-compose.yml` | OpenSearch :9200 + Neo4j :7474 |
| `scripts/generate_data.py` | Creates all 4 data files in `data/` |
| `scripts/seed_stores.py` | Loads data into OpenSearch and Neo4j |
| `src/api/main.py` | FastAPI app — `POST /query`, `GET /metrics` |
| `src/retrieval/opensearch_retriever.py` | Hybrid dense+BM25, uses fastembed |
| `docs/arch.md` | Architecture + 3 full query traces |
| `docs/tradeoffs.md` | Design decisions and honest trade-offs |
| `INSTRUCTIONS.md` | This file |
