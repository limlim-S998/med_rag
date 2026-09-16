"""Reciprocal-rank fusion: combine dense and lexical ranks without score units."""


def rrf(rank_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    if k < 1:
        raise ValueError("RRF k must be positive")
    scores: dict[str, float] = {}
    for ranking in rank_lists:
        seen: set[str] = set()
        for rank, identifier in enumerate(ranking, start=1):
            if identifier not in seen:
                scores[identifier] = scores.get(identifier, 0.0) + 1 / (k + rank)
                seen.add(identifier)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))
