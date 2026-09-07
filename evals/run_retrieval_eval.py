# The golden set is the only reason any retrieval claim in this repo is
# defensible. ~180 queries the writers labelled by hand.
#
# The finding that bought the cross-encoder: recall@10 was fine, recall@3 was
# poor. Meaning the right chunk was in the candidate pool but not at the top -
# which is a ranking problem, not a retrieval problem, and reranking is the
# fix. If recall@10 had been the weak number, more reranking would have done
# nothing and the answer would have been chunking or the embedding.
#
# IMPLEMENTATION HELD BACK. Working version in holding/ and on the
# implementation/retrieval-slice branch, along with a populated golden set and
# a dense-only variant that needs no running service.

RETRIEVAL_URL = "http://localhost:8001/search"


async def evaluate(path: str, ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    # Returns {"recall@k": float, ..., "mrr": float, "n": int}.
    ...
