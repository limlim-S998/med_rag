from app.fusion import rrf


def test_rrf_rewards_agreement():
    dense = ["a", "b", "c"]
    sparse = ["c", "a", "z"]
    out = dict(rrf([dense, sparse], k=60))
    # "a" is rank 1 and 2; "c" is rank 3 and 1. Both beat single-list entries.
    assert out["a"] > out["b"]
    assert out["c"] > out["z"]


def test_rrf_is_scale_free():
    # The point of RRF: only ranks matter, so it cannot be gamed by one
    # retriever having a larger score range.
    assert dict(rrf([["a", "b"]], k=60)) == dict(rrf([["a", "b"]], k=60))
