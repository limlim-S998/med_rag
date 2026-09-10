"""A deterministic test double; no learned model or clinical quality claim."""


class SyntheticReranker:
    async def rerank(self, query: str, candidates: list[tuple[str, str]], *, top_k: int):
        terms = set(query.lower().split())
        scored = [
            (identifier, float(len(terms.intersection(text.lower().split()))))
            for identifier, text in candidates
        ]
        return sorted(scored, key=lambda row: (-row[1], row[0]))[:top_k]

    async def check(self) -> None:
        return None
