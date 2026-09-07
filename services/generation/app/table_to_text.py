# The load-bearing anti-hallucination design.
#
# Naive: hand the table to the LLM and ask for a paragraph. It will
# paraphrase a number wrong eventually, and a wrong efficacy number in a
# submission document is not a typo, it is a finding.
#
# So: the numeric spine is a template filled from the parsed table. The model
# is only allowed to write connective prose, and it never sees a slot it can
# overwrite. The classifier picks which template - which is why that little
# LinearSVC in ml/ is load-bearing rather than decoration.
#
# IMPLEMENTATION HELD BACK. Templates and slot extraction in holding/ and on
# the implementation/retrieval-slice branch. The template *wording* is
# clinical content owned by the medical writers; what the scaffolding fixes is
# the contract: render() takes a ParsedTable and returns slots the
# verification layer can diff against.

from medw_core.schemas import ParsedTable, TableType

# One template per table type. Content lives with the writers, not here.
TEMPLATES: dict[TableType, str] = {}


def render(table: ParsedTable, table_type: TableType) -> dict:
    # Every value here comes out of ParsedTable.cells - never out of a model.
    # Returns {"skeleton", "slots", "source_table"}; `slots` is the set the
    # verification pass re-extracts the output against.
    ...


def _extract_slots(table: ParsedTable, table_type: TableType) -> dict:
    ...


# The LLM step that follows gets: the skeleton, and an instruction that it may
# smooth transitions and may not alter, add or remove any numeral. The
# verification pass then re-extracts every numeral from the output and diffs
# it against `slots`. Any mismatch fails the section - it does not warn.
