# Azure Document Intelligence gives you a layout model that returns tables as
# cells with row/column indices and spans. It does NOT give you the clinical
# structure: which rows are a header stack, which are a system-organ-class
# group, which footnote marker binds to which cell. That is this file.
#
# Naive path (what you must be able to describe as the thing you rejected):
#   text = di_result.content ; chunks = splitter.split(text)
# -> orphaned rows with no header. Worse than no retrieval, because a row of
# numbers with no header still looks like a confident answer.
#
# IMPLEMENTATION HELD BACK. Working version in holding/ and on the
# implementation/retrieval-slice branch. The parsing strategy is a modelling
# decision; the scaffolding only needs the signature and the domain types it
# produces.

import re

from medw_core.schemas import ParsedTable

TABLE_NUM = re.compile(r"Table\s+(\d+(?:\.\d+)+)")


def parse_di_table(di_table: dict, title_block: str, footnote_block: str) -> ParsedTable:
    # Header stack: column headers can span several rows (arm, then n=, then
    # unit). Concatenate down the column so each data column carries its full
    # path, e.g. "Placebo / n=142 / n (%)".
    ...


def _split_n_pct(value: str) -> dict:
    # "12 (8.5%)" -> n=12, pct=8.5. Keep the original string as the value:
    # the deterministic table-to-text renders the string, not a reconstruction.
    ...
