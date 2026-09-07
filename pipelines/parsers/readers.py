# LlamaIndex, and exactly where the line with LangChain falls.
#
# Split by layer, not by preference: LlamaIndex on the ingestion and indexing
# side (readers, node parsing, the Document/Node abstractions, metadata
# propagation to nodes), LangChain on the orchestration side (chains, the
# agent loop in generation). They overlap enough that carrying both is a fair
# thing to be challenged on - the honest answer is that it was two teams'
# starting points converging, and today it would be consolidated onto one.
#
# What LlamaIndex is actually doing here: the Document/Node model with
# metadata that survives into the payload, and the readers for the formats we
# do NOT parse ourselves (RTF from the SAS TFL output, plain protocol DOCX).
# The clinical tables never touch a stock node parser - they go through
# parsers/table.py, because a generic splitter is exactly what broke the first
# implementation.

from llama_index.core import Document
from llama_index.core.node_parser import NodeParser

from medw_core.schemas import Chunk, DocType


def read_rtf(path: str, study_id: str, doc_id: str) -> list[Document]:
    # TFLs arrive as RTF from SAS. Text extraction is the easy half; the
    # numbering and the header rows are the half that matters.
    ...


def read_protocol(path: str, study_id: str, doc_id: str) -> list[Document]:
    ...


class E3SectionNodeParser(NodeParser):
    """Small-to-big at the section level.

    Embeds a ~900-char window for precision, but attaches the enclosing E3
    section as the node's context so the generator receives surrounding
    narrative rather than a fragment. The metadata (study_id, doc_type,
    section_path, kind) is what becomes the Qdrant payload and the Cognitive
    Search filter fields, so it has to be set here, once.
    """

    def _parse_nodes(self, nodes, show_progress: bool = False, **kwargs):
        ...


def to_chunks(nodes, study_id: str, doc_id: str, doc_type: DocType) -> list[Chunk]:
    # Convert to our own schema at the boundary. The framework's types do not
    # cross into the services - medw_core.schemas.Chunk does. That is what
    # makes "we would consolidate onto one framework today" a refactor of this
    # file rather than of the whole repo.
    ...
