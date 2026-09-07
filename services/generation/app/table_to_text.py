# The load-bearing anti-hallucination design.
#
# Naive: hand the table to the LLM and ask for a paragraph. It will
# paraphrase a number wrong eventually, and a wrong efficacy number in a
# submission document is not a typo, it is a finding.
#
# So: the numeric spine is a template filled from the parsed table. The model
# is only allowed to write connective prose, and it never sees a slot it can
# overwrite. The classifier picks which template - which is why that little
# LinearSVC is load-bearing rather than decoration.

from medw_core.schemas import ParsedTable, TableType

TEMPLATES: dict[TableType, str] = {
    TableType.demographics: (
        "A total of {total_n} subjects were randomised. "
        "{arm_sentences} Demographic characteristics were {balance_word} across treatment groups."
    ),
    TableType.ae_summary: (
        "Treatment-emergent adverse events were reported in {teae_line}. "
        "The most frequently reported events by system organ class were {top_socs}."
    ),
}


def render(table: ParsedTable, table_type: TableType) -> dict:
    # Every value here comes out of ParsedTable.cells - never out of a model.
    slots = _extract_slots(table, table_type)
    skeleton = TEMPLATES[table_type].format(**slots)
    return {"skeleton": skeleton, "slots": slots, "source_table": table.table_number}


def _extract_slots(table: ParsedTable, table_type: TableType) -> dict:
    ...


# The LLM step that follows gets: the skeleton, and an instruction that it may
# smooth transitions and may not alter, add or remove any numeral. The
# verification pass then re-extracts every numeral from the output and diffs
# it against `slots`. Any mismatch fails the section - it does not warn.
