# Azure AI Language - clinical entity extraction with UMLS linking.
#
# Used at two points, and it is worth being precise about which:
#
#  1. Ingestion. Extract clinical entities from prose chunks and store them in
#     the payload as a keyword-indexed field. That gives the sparse half
#     something better than raw tokens to match on, and it gives the filter
#     side a way to say "chunks that mention this condition" without a second
#     embedding space.
#
#  2. Verification. Re-extract entities from the *generated* paragraph and
#     check that every coded term in the output also appears in the source
#     chunks. That is the coded-term half of verify.py; numeric_fidelity is
#     the numeric half.
#
# What it is NOT: a MedDRA coder. Azure Language links to UMLS CUIs, and
# MedDRA is a separate controlled vocabulary the sponsor licenses. The mapping
# from CUI to MedDRA PT is a lookup table the client owned - we consumed it.
# Saying "it does MedDRA coding" out loud would be wrong and a clinical
# reviewer would catch it.

from azure.ai.textanalytics.aio import TextAnalyticsClient

# The analyse call is long-running (a poller, not a request/response) and
# batched at 25 documents. Ingestion batches; the verification path calls it
# with a single document and eats the poll latency, which is why verification
# is a background pass over a drafted section rather than an inline gate.
MAX_BATCH = 25


async def extract_entities(client: TextAnalyticsClient, texts: list[str]) -> list[list[dict]]:
    # Returns, per input document, the entities with category, confidence, and
    # any UMLS links. We keep: text, category, normalized_text, CUI.
    ...


def coded_terms(entities: list[dict]) -> set[str]:
    # Flattened to the set used by the verification diff.
    ...
