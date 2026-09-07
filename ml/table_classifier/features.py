# The feature surface is deliberately tiny: the title, the header stack and
# the footnotes. Not the cell values - those vary per study, and a classifier
# that keys on them will not generalise to the next sponsor's table shells.
#
# IMPLEMENTATION HELD BACK (one line, in holding/). Feature choice is the
# modelling decision this file exists to record.


def table_to_document(title: str, header_stack: list[str], footnotes: list[str]) -> str:
    ...
