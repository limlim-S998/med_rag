# Azure Document Intelligence gives you a layout model that returns tables as
# cells with row/column indices and spans. It does NOT give you the clinical
# structure: which rows are a header stack, which are a system-organ-class
# group, which footnote marker binds to which cell. That is this file.
#
# Naive path (what you must be able to describe as the thing you rejected):
#   text = di_result.content ; chunks = splitter.split(text)
# -> orphaned rows with no header. Worse than no retrieval, because a row of
# numbers with no header still looks like a confident answer.

import re

from medw_core.schemas import ParsedTable, TableCell

TABLE_NUM = re.compile(r"Table\s+(\d+(?:\.\d+)+)")


def parse_di_table(di_table: dict, title_block: str, footnote_block: str) -> ParsedTable:
    cells = di_table["cells"]
    header_rows = sorted({c["rowIndex"] for c in cells if c.get("kind") == "columnHeader"})
    n_header = (max(header_rows) + 1) if header_rows else 1

    # Header stack: column headers can span several rows (arm, then n=, then
    # unit). Concatenate down the column so each data column carries its full
    # path, e.g. "Placebo / n=142 / n (%)".
    ncols = max(c["columnIndex"] for c in cells) + 1
    stack = [""] * ncols
    for c in cells:
        if c["rowIndex"] < n_header:
            for j in range(c["columnIndex"], c["columnIndex"] + c.get("columnSpan", 1)):
                stack[j] = (stack[j] + " / " + c["content"]).strip(" /")

    out: list[TableCell] = []
    by_row: dict[int, dict[int, str]] = {}
    for c in cells:
        if c["rowIndex"] >= n_header:
            by_row.setdefault(c["rowIndex"], {})[c["columnIndex"]] = c["content"]

    for _, row in sorted(by_row.items()):
        label = row.get(0, "")
        for col, val in row.items():
            if col == 0:
                continue
            out.append(TableCell(row_label=label, arm=stack[col], value=val, **_split_n_pct(val)))

    m = TABLE_NUM.search(title_block)
    return ParsedTable(
        table_number=m.group(0) if m else "unknown",
        title=title_block.strip(),
        header_stack=stack,
        footnotes=[f.strip() for f in footnote_block.splitlines() if f.strip()],
        cells=out,
    )


def _split_n_pct(value: str) -> dict:
    # "12 (8.5%)" -> n=12, pct=8.5. Keep the original string as the value:
    # the deterministic table-to-text renders the string, not a reconstruction.
    m = re.match(r"^\s*(\d+)\s*\(\s*([\d.]+)\s*%\s*\)\s*$", value)
    if not m:
        return {}
    return {"n": int(m.group(1)), "pct": float(m.group(2))}
