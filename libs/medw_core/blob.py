# Azure Blob Storage - where the bytes live.
#
# Three containers, and the separation is the point:
#
#   raw/       what the writer uploaded, byte-identical, never mutated.
#   parsed/    Document Intelligence output + our ParsedTable JSON, keyed by
#              parser version. A parser fix writes a NEW prefix; it does not
#              overwrite the old one, so you can diff two parser versions over
#              the same source without re-running Document Intelligence (which
#              is the expensive call in the pipeline, per page, in money).
#   snapshots/ nightly Qdrant snapshots. Qdrant on a PVC is not a backup.
#
# Path convention (deterministic, same inputs -> same path, like the chunk IDs):
#   raw/{study_id}/{doc_id}/{filename}
#   parsed/{study_id}/{doc_id}/{parser_version}/layout.json
#   snapshots/{collection}/{date}.snapshot

from azure.storage.blob.aio import BlobServiceClient

from medw_core.ids import PARSER_VERSION

RAW = "raw"
PARSED = "parsed"
SNAPSHOTS = "snapshots"


def raw_path(study_id: str, doc_id: str, filename: str) -> str:
    return f"{study_id}/{doc_id}/{filename}"


def parsed_path(study_id: str, doc_id: str, artifact: str = "layout.json") -> str:
    return f"{study_id}/{doc_id}/{PARSER_VERSION}/{artifact}"


async def put_json(bsc: BlobServiceClient, container: str, path: str, payload: bytes) -> None:
    ...


async def get_bytes(bsc: BlobServiceClient, container: str, path: str) -> bytes:
    ...


# Retention is a storage lifecycle policy, not application code: raw/ moves to
# cool after 90 days and is never deleted (it is the source of record for a
# reprocess), parsed/ expires versions older than the two most recent parser
# versions. Client data leaves when the study is torn down, and that teardown
# is one container delete plus one Qdrant collection delete - which is exactly
# why per-study is the partitioning unit everywhere in this repo.
