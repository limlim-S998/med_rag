# Chunking is where clinical RAG is won or lost.
#
# Tables: chunk at row-group level, and prepend the table number, title,
# population, and the full header stack to every chunk. Every chunk must be
# self-describing, because at retrieval time it arrives with no neighbours.
#
# Prose: small-to-big. Embed a small window (better precision, the embedding
# is not diluted), but return the whole E3 section as context (the generator
# needs the surrounding narrative).

from medw_core.ids import chunk_id
from medw_core.schemas import Chunk, DocType, ParsedTable

ROWS_PER_CHUNK = 12


def chunk_table(t: ParsedTable, study_id: str, doc_id: str, section_path: str) -> list[Chunk]:
    row_labels: list[str] = []
    for c in t.cells:
        if c.row_label not in row_labels:
            row_labels.append(c.row_label)

    preamble = "\n".join([
        t.table_number,
        t.title,
        f"Population: {t.population}" if t.population else "",
        "Columns: " + " | ".join(t.header_stack),
    ]).strip()

    chunks = []
    for i in range(0, len(row_labels), ROWS_PER_CHUNK):
        group = row_labels[i:i + ROWS_PER_CHUNK]
        body = "\n".join(
            f"{c.row_label} | {c.arm} | {c.value}" for c in t.cells if c.row_label in group
        )
        footer = "\n".join(t.footnotes)
        ordinal = i // ROWS_PER_CHUNK
        chunks.append(Chunk(
            id=chunk_id(study_id, doc_id, section_path, ordinal),
            study_id=study_id, doc_id=doc_id, doc_type=DocType.tfl,
            section_path=section_path, kind="table_rows",
            text=f"{preamble}\n\n{body}\n\n{footer}".strip(),
            table=t,                       # structured spine rides in the payload
            ordinal=ordinal,
        ))
    return chunks


def chunk_prose(sections: dict[str, str], study_id: str, doc_id: str,
                doc_type: DocType, window: int = 900, stride: int = 700) -> list[Chunk]:
    chunks = []
    for section_path, text in sections.items():
        for ordinal, start in enumerate(range(0, max(len(text), 1), stride)):
            piece = text[start:start + window]
            if not piece.strip():
                continue
            chunks.append(Chunk(
                id=chunk_id(study_id, doc_id, section_path, ordinal),
                study_id=study_id, doc_id=doc_id, doc_type=doc_type,
                section_path=section_path, kind="prose",
                text=f"[{section_path}] {piece}",
                ordinal=ordinal,
            ))
    return chunks
