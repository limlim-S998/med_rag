# The learned reranker the cross-encoder had to beat.
#
# Features per (query, chunk): BM25 score, dense cosine, section-path match,
# table-vs-prose flag, recency. LightGBM with a ranking objective over the
# golden set. It was never shipped - but it is the reason shipping a
# cross-encoder was a decision rather than a default.

import lightgbm as lgb


def train(X, y, group_sizes):
    return lgb.LGBMRanker(
        objective="lambdarank",
        metric="ndcg",
        n_estimators=400,
        learning_rate=0.05,
    ).fit(X, y, group=group_sizes)
