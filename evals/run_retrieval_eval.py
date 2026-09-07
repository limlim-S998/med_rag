# The golden set is the only reason any retrieval claim in this repo is
# defensible. ~180 queries the writers labelled by hand.
#
# The finding that bought the cross-encoder: recall@10 was fine, recall@3 was
# poor. Meaning the right chunk was in the candidate pool but not at the top -
# which is a ranking problem, not a retrieval problem, and reranking is the
# fix. If recall@10 had been the weak number, more reranking would have done
# nothing and the answer would have been chunking or the embedding.

import asyncio
import json
import statistics

import httpx

RETRIEVAL_URL = "http://localhost:8001/search"


def load_golden_set(path: str) -> list[dict]:
    # Read up front, synchronously, outside the event loop. Blocking file I/O
    # inside an async function stalls every other task on that loop - harmless
    # for one small file here, and exactly the habit that is not harmless in
    # the services.
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def evaluate(path: str, ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    queries = load_golden_set(path)
    hits_at = {k: [] for k in ks}
    mrr = []

    async with httpx.AsyncClient(timeout=30) as http:
        for q in queries:
            r = await http.post(RETRIEVAL_URL, json={
                "study_id": q["study_id"], "query": q["query"], "top_k": max(ks)})
            ranked = [h["chunk_id"] for h in r.json()["hits"]]
            relevant = set(q["relevant_chunk_ids"])
            for k in ks:
                hits_at[k].append(int(bool(relevant & set(ranked[:k]))))
            rank = next((i for i, c in enumerate(ranked, 1) if c in relevant), None)
            mrr.append(1 / rank if rank else 0.0)

    return {**{f"recall@{k}": statistics.mean(v) for k, v in hits_at.items()},
            "mrr": statistics.mean(mrr), "n": len(queries)}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(evaluate("evals/golden_set.jsonl")), indent=2))
