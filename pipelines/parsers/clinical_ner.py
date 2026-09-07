# Clinical entity extraction at ingest time, via Azure AI Language.
#
# The output goes into one field, `coded_terms`, which is indexed as a keyword
# collection in Cognitive Search (see infra/search/csr-chunks-index.json) and
# as a payload field in Qdrant. It exists because the sparse half needs to
# match clinical vocabulary literally, and because the verification pass needs
# a set of terms it can diff.
#
# Applied to prose chunks only. Table chunks already carry their coded terms
# in the row labels, and running NER over "12 (8.5%)" produces noise.

from medw_core.language import MAX_BATCH, coded_terms, extract_entities  # noqa: F401
from medw_core.schemas import Chunk

# Categories worth keeping. Azure Language's healthcare model emits ~30; most
# of them (Frequency, Time, Course) are narrative colour that would dilute the
# keyword index.
KEEP = {
    "Diagnosis",
    "SymptomOrSign",
    "MedicationName",
    "MedicationClass",
    "TreatmentName",
    "ExaminationName",
    "Dosage",
    "AdministrativeEvent",
}


async def annotate(client, chunks: list[Chunk]) -> list[Chunk]:
    # Batched at MAX_BATCH, and it is a poller per batch - this is the slowest
    # step in ingestion after Document Intelligence, which is fine for a batch
    # DAG and is why the synchronous single-document worker can skip it and
    # backfill later.
    ...
