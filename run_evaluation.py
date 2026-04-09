"""
Evaluation runner — runs all 10 test questions against the live agent.

Usage:
  python run_evaluation.py --questions all --output my_answers.json
  python run_evaluation.py --questions Q1,Q3,Q5
  python run_evaluation.py --questions all --compare-expected

Before running:
  1. docker-compose up -d
  2. python scripts/generate_data.py
  3. python scripts/seed_stores.py
  4. Set ANTHROPIC_API_KEY in .env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"

QUESTIONS = [
    {"id": "Q1",  "question": "What is the payment service responsible for?",        "difficulty": "low"},
    {"id": "Q2",  "question": "What deployments happened in the last 24 hours?",       "difficulty": "low"},
    {"id": "Q3",  "question": "Which services does checkout-svc depend on?",           "difficulty": "low"},
    {"id": "Q4",  "question": "What incidents involved auth-svc last month?",          "difficulty": "medium"},
    {"id": "Q5",  "question": "Is payment-svc healthy right now?",                     "difficulty": "medium"},
    {"id": "Q6",  "question": "What changed before the last 3 major incidents?",       "difficulty": "hard"},
    {"id": "Q7",  "question": "Which service is the most fragile in our system?",      "difficulty": "hard"},
    {"id": "Q8",  "question": "Why does checkout slow down every Friday evening?",     "difficulty": "hard"},
    {"id": "Q9",  "question": "Compare how we resolved the last 5 payment incidents",  "difficulty": "very_hard"},
    {"id": "Q10", "question": "What don't we know about today's incident?",            "difficulty": "expert"},
]


async def run_single_question(
    question_id: str,
    question: str,
    orchestrator,
    query_agent,
    deepener,
    generator,
) -> dict:
    """Run one question through the full pipeline and return the response dict."""
    print(f"  [{question_id}] {question[:60]}...")
    start = time.time()

    try:
        # Layer 1
        query_plan = await query_agent.parse(question)

        # Layer 2 (initial retrieval)
        initial_evidence, initial_hit_map = await orchestrator.retrieve(query_plan)

        # Layer 3 (iterative deepening)
        evidence, iterations_used, hit_map = await deepener.run(
            query_plan, initial_evidence, initial_hit_map
        )

        # Layer 4 (response generation)
        response = await generator.generate(
            query=question,
            evidence=evidence,
            query_plan=query_plan,
            question_id=question_id,
            start_time_ms=int(start * 1000),
        )

        elapsed = time.time() - start
        print(f"         ✓ {elapsed:.1f}s | {iterations_used} iterations | {len(evidence.sources)} sources | confidence={response.confidence:.2f}")

        return response.model_dump(mode="json")

    except Exception as e:
        print(f"         ✗ ERROR: {e}")
        return {
            "question_id": question_id,
            "question": question,
            "error": str(e),
            "answer": "",
            "confidence": 0.0,
            "iterations_used": 0,
            "sources": [],
            "claims": [],
            "knowledge_gaps": [],
        }


def score_response(response: dict, expected: dict) -> int:
    """
    Score a single response against expected output.
    Q1-Q5: auto-scored (4 pts each)
    Q6-Q9: manual review (6 pts each) — we return 0 here
    Q10: manual review (10 pts) — we return 0 here
    """
    qid = response.get("question_id", "")

    if qid in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        # Auto-score: check if key facts appear in the answer
        answer = response.get("answer", "").lower()
        key_facts = expected.get("key_facts", [])
        hits = sum(
            1 for fact in key_facts
            if any(word.lower() in answer for word in fact.split()[:3])
        )
        # Score 4 if > 50% key facts present, 2 if > 25%, 0 otherwise
        fraction = hits / max(len(key_facts), 1)
        if fraction >= 0.5:
            return 4
        elif fraction >= 0.25:
            return 2
        return 0

    # Q6-Q10 require manual review
    return -1  # -1 = pending manual review


async def main(args: argparse.Namespace) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in.")
        return

    # Import here to avoid slow imports on --help
    from src.agents.iterative_deepening import IterativeDeepener
    from src.agents.query_understanding import QueryUnderstandingAgent
    from src.agents.response_generator import ResponseGenerator
    from src.agents.retrieval_orchestrator import RetrievalOrchestrator
    from src.retrieval.melt_retriever import MELTRetriever
    from src.retrieval.neo4j_retriever import Neo4jRetriever, make_neo4j_driver
    from src.retrieval.opensearch_retriever import OpenSearchRetriever, make_opensearch_client

    print("BugRaid ITOps Research Agent — Evaluation Runner")
    print("=" * 55)

    # Initialize connections
    print("\nConnecting to data stores...")
    os_client = await make_opensearch_client(
        host=os.environ.get("OPENSEARCH_HOST", "localhost"),
        port=int(os.environ.get("OPENSEARCH_PORT", "9200")),
    )
    neo4j_driver = await make_neo4j_driver(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", "bugraidpassword"),
    )
    melt_path = os.environ.get("MELT_DATA_PATH", "data/melt_telemetry.json")
    melt = MELTRetriever(melt_path)
    melt.load()

    orchestrator = RetrievalOrchestrator(OpenSearchRetriever(os_client), Neo4jRetriever(neo4j_driver), melt)
    query_agent = QueryUnderstandingAgent(api_key)
    deepener = IterativeDeepener(orchestrator, api_key)
    generator = ResponseGenerator(api_key)
    print("  ✓ Connected\n")

    # Determine which questions to run
    if args.questions == "all":
        questions_to_run = QUESTIONS
    else:
        requested = set(args.questions.upper().split(","))
        questions_to_run = [q for q in QUESTIONS if q["id"] in requested]

    print(f"Running {len(questions_to_run)} question(s):\n")

    # Run all questions
    answers = []
    for q in questions_to_run:
        answer = await run_single_question(
            q["id"], q["question"],
            orchestrator, query_agent, deepener, generator,
        )
        answers.append(answer)

    # Save output
    output_path = Path(args.output)
    output_path.write_text(json.dumps(answers, indent=2))
    print(f"\nAnswers saved to {output_path}")

    # Score if requested
    if args.compare_expected:
        expected_file = DATA_DIR / "expected_outputs.json"
        if not expected_file.exists():
            print("expected_outputs.json not found — run generate_data.py first")
        else:
            expected = {e["question_id"]: e for e in json.loads(expected_file.read_text())}
            print("\n" + "─" * 55)
            print("Auto-scoring Q1-Q5:")
            total_auto = 0
            for answer in answers:
                qid = answer.get("question_id", "")
                if qid in expected:
                    score = score_response(answer, expected[qid])
                    if score >= 0:
                        total_auto += score
                        print(f"  {qid}: {score}/4 pts")
                    else:
                        print(f"  {qid}: manual review required")
            print(f"\nAuto-score total: {total_auto}/20 pts")
            print("Q6-Q10 require manual review by BugRaid team")

    # Cleanup
    await os_client.close()
    await neo4j_driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BugRaid evaluation runner")
    parser.add_argument("--questions", default="all", help="Which questions to run: 'all' or 'Q1,Q2,Q5'")
    parser.add_argument("--output",   default="my_answers.json", help="Output file path")
    parser.add_argument("--compare-expected", action="store_true", help="Compare against expected_outputs.json")
    args = parser.parse_args()
    asyncio.run(main(args))
