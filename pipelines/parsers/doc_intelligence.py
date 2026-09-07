# Azure Document Intelligence - the extraction call itself.
#
# Model choice: `prebuilt-layout`, not `prebuilt-document`. Layout returns the
# geometry - tables as cells with row/column indices and spans, paragraphs with
# roles, selection marks. prebuilt-document adds key-value extraction that is
# tuned for invoices and forms and is worse than useless on a TFL package.
# Everything clinical comes from our own parsers on top of the geometry, which
# is what pipelines/parsers/table.py is.
#
# It is a long-running operation: submit, poll, collect. And it is the
# expensive call in the pipeline - priced per page, and a TFL package is
# hundreds of pages - which is the whole reason parsed output is cached in
# Blob under a parser-version prefix. A parser fix re-runs OUR code over
# cached geometry; it does not re-run this.

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

from medw_core.settings import Settings

# Pages per request. Above this the operation gets slow enough that a worker
# holding it open is a bad use of a pod; the DAG splits and reassembles.
PAGE_BATCH = 50


async def analyze_layout(client: DocumentIntelligenceClient, s: Settings,
                         blob_url: str, pages: str | None = None) -> dict:
    # url_source, not a byte stream: Document Intelligence reads the blob
    # directly using its own managed identity, so the PDF never travels
    # through our pod. One less place client data sits in memory, and it is
    # the difference between a 300MB pod and a 300MB stream.
    poller = await client.begin_analyze_document(
        s.docintel_model,
        AnalyzeDocumentRequest(url_source=blob_url),
        pages=pages,
    )
    result = await poller.result()
    return result.as_dict()


def split_tables_and_prose(layout: dict) -> tuple[list[dict], dict[str, str]]:
    # Layout gives you `tables` and `paragraphs` separately, but the title and
    # footnotes belonging to a table are paragraphs, not part of the table
    # object. Binding them is by page + bounding-box proximity: the caption
    # above, the footnote block below, both within the same page region.
    #
    # This is the unglamorous part that decides whether retrieval works. A
    # chunk that arrives without its table number and header stack is a row of
    # numbers that still looks like a confident answer.
    ...


def section_paths(layout: dict) -> dict[str, str]:
    # Paragraph roles include `title` and `sectionHeading`. Walk them in
    # document order maintaining a heading stack to recover the E3 numbering
    # ('11.4.2'), which every chunk carries and every filter uses.
    ...
