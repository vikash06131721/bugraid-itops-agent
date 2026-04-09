"""
Seeds OpenSearch and Neo4j with the synthetic data files.

Run this after docker-compose up and after generate_data.py:
  python scripts/seed_stores.py

What it does:
  1. Creates the OpenSearch index with hybrid (kNN + BM25) mappings
  2. Bulk-indexes all 500 incidents with embeddings
  3. Runs neo4j_seed.cypher to populate the graph
  4. Prints a summary so you know it worked

Expected runtime: 3-5 minutes (embedding 500 documents takes a moment)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from opensearchpy import AsyncOpenSearch, helpers

load_dotenv()

DATA_DIR = Path(__file__).parent.parent / "data"
INDEX_NAME = "bugraid-incidents"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Embedding helpers — fastembed uses ONNX runtime, no PyTorch.
# This avoids the libc++ mutex crash on macOS 26 / PyTorch 2.8.
# ---------------------------------------------------------------------------

_embed_model = None


def _load_embedding_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        print("  Loading embedding model via fastembed (ONNX, no PyTorch)...")
        _embed_model = TextEmbedding(EMBEDDING_MODEL)
        print("  Embedding model loaded ✓")
    return _embed_model


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Returns list of 384-dim normalised float vectors."""
    model = _load_embedding_model()
    return [list(v) for v in model.embed(texts)]

OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT = int(os.environ.get("OPENSEARCH_PORT", "9200"))
NEO4J_URI       = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER      = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD  = os.environ.get("NEO4J_PASSWORD", "bugraidpassword")


# ---------------------------------------------------------------------------
# OpenSearch seeding
# ---------------------------------------------------------------------------

INDEX_MAPPING = {
    "settings": {
        "index": {"knn": True, "knn.algo_param.ef_search": 100},
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "incident_id":       {"type": "keyword"},
            "title":             {"type": "text", "analyzer": "standard"},
            "severity":          {"type": "keyword"},
            "service":           {"type": "keyword"},
            "timestamp":         {"type": "date"},
            "duration_minutes":  {"type": "integer"},
            "affected_services": {"type": "keyword"},
            "rca_summary":       {"type": "text"},
            "root_cause":        {"type": "keyword"},
            "resolution":        {"type": "text"},
            "tags":              {"type": "keyword"},
            "resolved":          {"type": "boolean"},
            # The embedding field — 384 dims from all-MiniLM-L6-v2
            "embedding": {
                "type": "knn_vector",
                "dimension": 384,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "nmslib",
                },
            },
        }
    },
}


def make_embedding_text(incident: dict) -> str:
    """Concatenate the most meaningful fields for embedding."""
    parts = [
        incident.get("title", ""),
        incident.get("rca_summary", ""),
        incident.get("root_cause", "").replace("_", " "),
        incident.get("resolution", ""),
        " ".join(incident.get("tags", [])),
    ]
    return " ".join(p for p in parts if p)


async def seed_opensearch() -> None:
    print("\n[OpenSearch] Starting...")
    client = AsyncOpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        http_compress=True,
        use_ssl=False,
        verify_certs=False,
        timeout=60,
    )

    # Load embedding model (triggers download if needed)
    _load_embedding_model()

    # Delete and recreate index
    if await client.indices.exists(index=INDEX_NAME):
        await client.indices.delete(index=INDEX_NAME)
        print(f"  Deleted existing index '{INDEX_NAME}'")

    await client.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
    print(f"  Created index '{INDEX_NAME}' with kNN + BM25 mappings")

    # Load incidents
    incidents_file = DATA_DIR / "synthetic_incidents.jsonl"
    if not incidents_file.exists():
        print(f"  ERROR: {incidents_file} not found. Run generate_data.py first.")
        await client.close()
        return

    incidents = [json.loads(line) for line in incidents_file.read_text().strip().split("\n") if line]
    print(f"  Embedding {len(incidents)} incidents...")

    # Embed in batches
    BATCH = 32  # smaller batch — safer on CPU
    docs = []
    for i in range(0, len(incidents), BATCH):
        batch = incidents[i : i + BATCH]
        texts = [make_embedding_text(inc) for inc in batch]
        embeddings = embed_batch(texts)

        for inc, emb in zip(batch, embeddings):
            doc = dict(inc)
            doc["embedding"] = emb
            docs.append({
                "_index": INDEX_NAME,
                "_id": inc["incident_id"],
                "_source": doc,
            })

        progress = min(i + BATCH, len(incidents))
        print(f"  Embedded {progress}/{len(incidents)}...", end="\r")

    print()

    # Bulk index
    success, failed = await helpers.async_bulk(client, docs, raise_on_error=False)
    print(f"  Indexed {success} documents, {len(failed)} failed")

    await client.close()
    print("[OpenSearch] Done ✓")


# ---------------------------------------------------------------------------
# Neo4j seeding
# ---------------------------------------------------------------------------

async def seed_neo4j() -> None:
    print("\n[Neo4j] Starting...")
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        print("  ERROR: neo4j package not installed")
        return

    cypher_file = DATA_DIR / "neo4j_seed.cypher"
    if not cypher_file.exists():
        print(f"  ERROR: {cypher_file} not found. Run generate_data.py first.")
        return

    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # Test connectivity
    try:
        await driver.verify_connectivity()
        print("  Connected to Neo4j")
    except Exception as e:
        print(f"  ERROR: Cannot connect to Neo4j at {NEO4J_URI}: {e}")
        await driver.close()
        return

    # Split Cypher file into individual statements
    raw = cypher_file.read_text()
    statements = [
        s.strip()
        for s in raw.split(";")
        if s.strip() and not s.strip().startswith("//")
    ]

    print(f"  Running {len(statements)} Cypher statements...")
    success = 0
    errors = 0

    async with driver.session() as session:
        for i, stmt in enumerate(statements):
            if not stmt:
                continue
            try:
                await session.run(stmt)
                success += 1
            except Exception as e:
                errors += 1
                if errors <= 3:  # only print first few errors
                    print(f"  WARN: Statement {i} failed: {str(e)[:100]}")

    # Verify counts
    async with driver.session() as session:
        result = await session.run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC")
        counts = {record["label"]: record["count"] async for record in result}
        print(f"  Graph nodes: {counts}")

    await driver.close()
    print(f"[Neo4j] Done ✓ ({success} statements succeeded, {errors} failed)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("BugRaid Data Seeding")
    print("=" * 40)
    print("Make sure docker-compose is running:")
    print("  docker-compose up -d")
    print("  curl http://localhost:9200/_cluster/health  # should be green/yellow")
    print()

    await seed_opensearch()
    await seed_neo4j()

    print("\n" + "=" * 40)
    print("Seeding complete! You can now start the agent:")
    print("  uvicorn src.api.main:app --reload")
    print()
    print("Test it:")
    print('  curl -N http://localhost:8000/query \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"question": "Is payment-svc healthy?", "question_id": "Q5"}\'')


if __name__ == "__main__":
    asyncio.run(main())
