# Reciprocal Rank Fusion.
#
# Why not just add the scores: a cosine similarity lives in [-1, 1] and is
# tightly clustered near the top; a BM25 score is unbounded and corpus
# dependent. There is no normalisation that is stable across queries. RRF
# throws the scores away and keeps only the ranks, which is exactly the
# information that transfers.
#
#   score(d) = sum over lists of 1 / (k + rank(d))    k around 60
#
# k dampens the top: with k=60 the gap between rank 1 and rank 2 is small, so
# one list being confidently wrong cannot dominate.

from collections import defaultdict


def rrf(rank_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for lst in rank_lists:
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
