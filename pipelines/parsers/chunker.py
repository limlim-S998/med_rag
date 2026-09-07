# Chunking is where clinical RAG is won or lost.
#
# Tables: chunk at row-group level, and prepend the table number, title,
# population, and the full header stack to every chunk. Every chunk must be
# self-describing, because at retrieval time it arrives with no neighbours.
#
# Prose: small-to-big. Embed a small window (better precision, the embedding
# is not diluted), but return the whole E3 section as context (the generator
# needs the surrounding narrative).
#
# IMPLEMENTATION HELD BACK. Working version in holding/ and on the
# implementation/retrieval-slice branch. Chunk *size* and *strategy* are
# modelling decisions to tune against the golden set; what the scaffolding
# fixes is that both functions emit medw_core.schemas.Chunk with a
# deterministic ID.

from medw_core.schemas import Chunk, DocType, ParsedTable

ROWS_PER_CHUNK = 12


def chunk_table(t: ParsedTable, study_id: str, doc_id: str,
                section_path: str) -> list[Chunk]:
    ...


def chunk_prose(sections: dict[str, str], study_id: str, doc_id: str,
                doc_type: DocType, window: int = 900, stride: int = 700) -> list[Chunk]:
    ...
